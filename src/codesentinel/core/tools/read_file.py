"""read_file tool.

The `path` argument is the only thing the model controls. The sandbox
(`SandboxConfig`, i.e. which repo root this run is confined to) is bound
via closure in `make_read_file_tool` at tool-construction time — never
exposed as a model-visible parameter, or the model could point the tool at
an arbitrary repo root.
"""

from __future__ import annotations

from anthropic import beta_tool

from codesentinel.core.sandbox import SandboxConfig, SandboxViolation, resolve_within_root

# A single huge file read would otherwise blow the context/cost budget
# silently. Fail loudly and tell the model to slice instead of truncating
# without saying so.
MAX_FILE_BYTES = 200_000


def read_file_impl(
    path: str,
    config: SandboxConfig,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Pure implementation, independent of the `@beta_tool` wrapper — this
    is what tests call directly. `start_line`/`end_line` are 1-indexed and
    inclusive, matching how a human would describe "lines 10 to 20".
    """
    try:
        resolved = resolve_within_root(path, config.repo_root)
    except SandboxViolation as exc:
        return f"Error: {exc}"

    if not resolved.exists():
        return f"Error: {path!r} does not exist under the repository root."
    if not resolved.is_file():
        return f"Error: {path!r} is not a regular file."

    size = resolved.stat().st_size
    if size > MAX_FILE_BYTES:
        return (
            f"Error: {path!r} is {size} bytes, over the {MAX_FILE_BYTES}-byte "
            "read_file limit. Pass start_line/end_line to read a slice instead."
        )

    text = resolved.read_text(errors="replace")

    if start_line is None and end_line is None:
        return text

    lines = text.splitlines(keepends=True)
    start_index = max((start_line or 1) - 1, 0)
    end_index = end_line if end_line is not None else len(lines)
    if start_index >= len(lines):
        return f"Error: start_line {start_line} is beyond the file's {len(lines)} lines."
    return "".join(lines[start_index:end_index])


def make_read_file_tool(config: SandboxConfig):
    """Build a read_file tool bound to `config`. Call once per review."""

    @beta_tool
    def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """Read a file from the repository, optionally a line range, to see
        context around a diff hunk (e.g. the function surrounding a changed
        line, or an import at the top of the file).

        Args:
            path: Path to the file, relative to the repository root.
            start_line: Optional 1-indexed first line to include.
            end_line: Optional 1-indexed last line to include (inclusive).
        """
        return read_file_impl(path, config, start_line, end_line)

    return read_file
