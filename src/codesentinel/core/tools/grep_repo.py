"""grep_repo tool.

`pattern` and `path` are model-controlled; `config` (repo root, regex
timeout) is bound via closure — see the note in read_file.py, same
reasoning applies here. Every regex match runs under `safe_regex_search`'s
timeout, since `pattern` is effectively attacker-influenceable (it's
chosen by the model in response to diff content it doesn't control).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from anthropic import beta_tool

from codesentinel.core.sandbox import (
    RegexTimeout,
    SandboxConfig,
    SandboxViolation,
    resolve_within_root,
    safe_regex_search,
)

# Bounds so a search over a large or unexpected repo can't run forever or
# return an unusably huge result.
MAX_FILES_SCANNED = 2_000
MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_RESULTS = 50
HARD_MAX_RESULTS = 500

_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
)


def _iter_searchable_files(root: Path):
    """Yield candidate files under `root`, skipping VCS/build/venv dirs and
    anything too large to be worth scanning. Silently skips oversized files
    (unlike read_file, which errors loudly) — this is a search, not a
    request for one specific file's content.
    """
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            file_path = Path(dirpath) / filename
            try:
                if file_path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield file_path


def grep_repo_impl(
    pattern: str,
    path: str,
    max_results: int,
    config: SandboxConfig,
) -> str:
    """Pure implementation, independent of the `@beta_tool` wrapper."""
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"Error: invalid regex {pattern!r}: {exc}"

    try:
        search_root = resolve_within_root(path, config.repo_root)
    except SandboxViolation as exc:
        return f"Error: {exc}"

    if not search_root.exists():
        return f"Error: {path!r} does not exist under the repository root."

    capped_max_results = max(1, min(max_results, HARD_MAX_RESULTS))

    results: list[str] = []
    files_scanned = 0
    for file_path in _iter_searchable_files(search_root):
        if files_scanned >= MAX_FILES_SCANNED:
            results.append(f"... stopped after scanning {MAX_FILES_SCANNED} files (too many to search exhaustively)")
            break
        files_scanned += 1

        try:
            content = file_path.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — not a match candidate

        rel = file_path.relative_to(config.repo_root)
        try:
            matches = safe_regex_search(compiled, content, timeout=config.regex_timeout_seconds)
        except RegexTimeout:
            results.append(f"{rel}: search timed out on this file (pattern too expensive) — skipped")
            continue

        if not matches:
            continue

        file_lines = content.splitlines()
        for match in matches:
            line_no = content.count("\n", 0, match.start()) + 1
            line_text = file_lines[line_no - 1].strip() if line_no - 1 < len(file_lines) else ""
            results.append(f"{rel}:{line_no}: {line_text}")
            if len(results) >= capped_max_results:
                return "\n".join(results) + f"\n... truncated at {capped_max_results} results"

    if not results:
        return "No matches found."
    return "\n".join(results)


def make_grep_repo_tool(config: SandboxConfig):
    """Build a grep_repo tool bound to `config`. Call once per review."""

    @beta_tool
    def grep_repo(pattern: str, path: str = ".", max_results: int = DEFAULT_MAX_RESULTS) -> str:
        """Search the repository for a regex pattern — e.g. to find other
        call sites of a function that changed, or check whether a renamed
        symbol still has references elsewhere.

        Args:
            pattern: Python regular expression to search for.
            path: Directory or file to search, relative to the repository
                root. Defaults to the whole repository.
            max_results: Maximum number of matching lines to return.
        """
        return grep_repo_impl(pattern, path, max_results, config)

    return grep_repo
