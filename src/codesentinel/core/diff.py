"""Git diff/patch parsing, built on `unidiff`.

Produces a small, review-oriented view of a diff: which files changed,
which lines were added/removed (with target-file line numbers, since
that's what a review comment needs to point at), and the raw hunk text to
hand the model directly. Caps are enforced explicitly — a diff over the
size/file-count limit gets a warning the caller can surface, not a silent
best-effort attempt that quietly degrades review quality.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unidiff import PatchSet
from unidiff.patch import PatchedFile

# Beyond these, review quality and cost both degrade badly enough that it
# needs to be visible to whoever's reading the output, not just absorbed.
MAX_DIFF_BYTES = 500_000
MAX_FILES = 50


@dataclass(frozen=True)
class ChangedFile:
    path: str
    source_path: str | None  # pre-rename path, only set when is_renamed
    is_added: bool
    is_removed: bool
    is_renamed: bool
    added_line_numbers: tuple[int, ...]  # 1-indexed, in the NEW file
    removed_line_numbers: tuple[int, ...]  # 1-indexed, in the OLD file
    hunks_text: str  # raw unified-diff hunks for this file only


@dataclass(frozen=True)
class ParsedDiff:
    files: tuple[ChangedFile, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _strip_git_prefix(path: str) -> str | None:
    """`unidiff`'s source_file/target_file carry the `a/`/`b/` prefix git
    diffs use (or `/dev/null` for add/delete). Return the bare repo-relative
    path, or None when there isn't one.
    """
    if path in ("/dev/null", ""):
        return None
    return path.split("/", 1)[1] if "/" in path else path


def _convert_file(patched_file: PatchedFile) -> ChangedFile:
    added_line_numbers: list[int] = []
    removed_line_numbers: list[int] = []
    for hunk in patched_file:
        for line in hunk:
            if line.is_added and line.target_line_no is not None:
                added_line_numbers.append(line.target_line_no)
            elif line.is_removed and line.source_line_no is not None:
                removed_line_numbers.append(line.source_line_no)

    source_path = (
        _strip_git_prefix(patched_file.source_file) if patched_file.is_rename else None
    )

    return ChangedFile(
        path=patched_file.path,
        source_path=source_path,
        is_added=patched_file.is_added_file,
        is_removed=patched_file.is_removed_file,
        is_renamed=patched_file.is_rename,
        added_line_numbers=tuple(added_line_numbers),
        removed_line_numbers=tuple(removed_line_numbers),
        hunks_text=str(patched_file),
    )


def parse_diff(diff_text: str) -> ParsedDiff:
    """Parse unified diff / `git diff` text into a `ParsedDiff`.

    Raises `unidiff.errors.UnidiffParseError` on malformed input — that is
    a caller-facing "bad diff file" error, not something to swallow here.
    """
    warnings: list[str] = []
    size = len(diff_text.encode("utf-8"))
    if size > MAX_DIFF_BYTES:
        warnings.append(
            f"diff is {size} bytes, over the {MAX_DIFF_BYTES}-byte review "
            "cap — review quality and cost may suffer on a diff this large"
        )

    patch_set = PatchSet(diff_text)
    files = [_convert_file(pf) for pf in patch_set]

    if len(files) > MAX_FILES:
        warnings.append(
            f"diff touches {len(files)} files, over the {MAX_FILES}-file "
            f"cap — only the first {MAX_FILES} will be reviewed"
        )
        files = files[:MAX_FILES]

    return ParsedDiff(files=tuple(files), warnings=tuple(warnings))
