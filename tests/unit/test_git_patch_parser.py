"""Unit tests for extraction of changed lines from GitHub patches."""

from app.scrapers.git_patch_parser import ChangedLine, ChangeKind, GitPatchParser
from app.scrapers.github_client import GitHubFilePatch


def file_patch(filename: str, patch: str | None) -> GitHubFilePatch:
    return GitHubFilePatch(filename, "modified", 1, 1, 2, patch)


def test_parse_extracts_only_changed_content_in_original_order() -> None:
    patch = (
        "diff --git a/README.md b/README.md\r\n"
        "index abc..def 100644\r\n"
        "--- a/README.md\r\n"
        "+++ b/README.md\r\n"
        "@@ -1,3 +1,4 @@ heading\r\n"
        " context\r\n"
        "-old row\r\n"
        "+new row\r\n"
        "+\r\n"
        "-\r\n"
        "\\ No newline at end of file"
    )

    result = GitPatchParser.parse(patch, filename="README.md")

    assert result.filename == "README.md"
    assert result.lines == (
        ChangedLine(ChangeKind.REMOVED, "old row"),
        ChangedLine(ChangeKind.ADDED, "new row"),
        ChangedLine(ChangeKind.ADDED, ""),
        ChangedLine(ChangeKind.REMOVED, ""),
    )
    assert result.added_lines == ("new row", "")
    assert result.removed_lines == ("old row", "")


def test_parse_preserves_content_that_itself_begins_with_diff_markers() -> None:
    result = GitPatchParser.parse(
        "+++actual plus\n---actual minus\n++++header-like\n----header-like"
    )

    assert result.added_lines == ("++actual plus", "+++header-like")
    assert result.removed_lines == ("--actual minus", "---header-like")


def test_parse_preserves_content_beginning_with_one_or_two_signs() -> None:
    result = GitPatchParser.parse("++content starts plus\n--content starts minus")

    assert result.added_lines == ("+content starts plus",)
    assert result.removed_lines == ("-content starts minus",)


def test_parse_handles_missing_and_truncated_patches() -> None:
    assert GitPatchParser.parse(None).lines == ()
    assert GitPatchParser.parse("").lines == ()
    assert GitPatchParser.parse("@@ -10 +10 @@\n-old\n+new").added_lines == ("new",)


def test_parse_file_filters_non_readmes_and_explicit_targets() -> None:
    root = file_patch("README.md", "+root")
    nested = file_patch("docs/README.mdx", "+nested")
    unrelated = file_patch("jobs.md", "+job")

    assert GitPatchParser.parse_file(unrelated) is None
    assert GitPatchParser.parse_file(nested, target_readme_paths={"README.md"}) is None
    assert GitPatchParser.parse_file(root, target_readme_paths={"README.md"}) is not None


def test_parse_files_preserves_matching_file_order() -> None:
    results = GitPatchParser.parse_files(
        (
            file_patch("jobs.md", "+ignored"),
            file_patch("README.md", None),
            file_patch("archive/README.md", "+kept"),
        )
    )

    assert tuple(result.filename for result in results) == ("README.md", "archive/README.md")
    assert results[0].lines == ()
    assert results[1].added_lines == ("kept",)
