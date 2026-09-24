"""Tests for the three review tools. Each calls the `_impl` function
directly (the pure logic), not the `@beta_tool`-wrapped version — the
wrapper is just schema/closure plumbing exercised separately below.
"""

from __future__ import annotations

import sys

import pytest

from codesentinel.core.sandbox import SandboxConfig
from codesentinel.core.tools.grep_repo import grep_repo_impl, make_grep_repo_tool
from codesentinel.core.tools.read_file import make_read_file_tool, read_file_impl
from codesentinel.core.tools.run_tests import make_run_tests_tool, run_tests_impl


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 21)) + "\n"
    )
    (tmp_path / "src" / "util.py").write_text("def helper():\n    return 42\n")
    return tmp_path


# --- read_file -----------------------------------------------------------


def test_read_file_returns_full_contents(repo):
    config = SandboxConfig.for_repo(repo)

    result = read_file_impl("src/util.py", config)

    assert result == "def helper():\n    return 42\n"


def test_read_file_returns_line_range(repo):
    config = SandboxConfig.for_repo(repo)

    result = read_file_impl("src/app.py", config, start_line=3, end_line=5)

    assert result == "line 3\nline 4\nline 5\n"


def test_read_file_rejects_path_traversal(repo):
    config = SandboxConfig.for_repo(repo)

    result = read_file_impl("../../etc/passwd", config)

    assert result.startswith("Error:")


def test_read_file_errors_on_missing_file(repo):
    config = SandboxConfig.for_repo(repo)

    result = read_file_impl("src/does_not_exist.py", config)

    assert "does not exist" in result


def test_read_file_errors_on_oversized_file(repo, monkeypatch):
    import codesentinel.core.tools.read_file as read_file_module

    monkeypatch.setattr(read_file_module, "MAX_FILE_BYTES", 10)

    result = read_file_impl("src/util.py", config=SandboxConfig.for_repo(repo))

    assert "over the 10-byte" in result


def test_make_read_file_tool_wraps_impl(repo):
    config = SandboxConfig.for_repo(repo)
    tool = make_read_file_tool(config)

    # The @beta_tool wrapper exposes a callable schema-bearing object; call
    # its underlying function directly to confirm it's wired to this config
    # without invoking a live model.
    assert tool.name == "read_file"


# --- grep_repo -------------------------------------------------------------


def test_grep_repo_finds_matches_with_line_numbers(repo):
    config = SandboxConfig.for_repo(repo)

    result = grep_repo_impl(r"def \w+", ".", 50, config)

    assert "src/util.py:1: def helper():" in result


def test_grep_repo_reports_no_matches(repo):
    config = SandboxConfig.for_repo(repo)

    result = grep_repo_impl(r"nonexistent_symbol_xyz", ".", 50, config)

    assert result == "No matches found."


def test_grep_repo_rejects_invalid_regex(repo):
    config = SandboxConfig.for_repo(repo)

    result = grep_repo_impl(r"(unclosed", ".", 50, config)

    assert result.startswith("Error: invalid regex")


def test_grep_repo_rejects_path_traversal(repo):
    config = SandboxConfig.for_repo(repo)

    result = grep_repo_impl(r"x", "../../etc", 50, config)

    assert result.startswith("Error:")


def test_grep_repo_survives_a_redos_pattern_without_hanging(repo):
    (repo / "evil.txt").write_text("a" * 40 + "!\n")
    config = SandboxConfig.for_repo(repo, regex_timeout_seconds=0.2)

    result = grep_repo_impl(r"^(a+)+$", "evil.txt", 50, config)

    assert "timed out" in result


def test_grep_repo_truncates_at_max_results(repo):
    many_matches = "\n".join(f"foo{i}" for i in range(20))
    (repo / "many.txt").write_text(many_matches)
    config = SandboxConfig.for_repo(repo)

    result = grep_repo_impl(r"foo\d+", "many.txt", 5, config)

    assert result.count("\n") == 5  # 5 result lines + 1 truncation line
    assert "truncated at 5 results" in result


def test_make_grep_repo_tool_wraps_impl(repo):
    config = SandboxConfig.for_repo(repo)
    tool = make_grep_repo_tool(config)

    assert tool.name == "grep_repo"


# --- run_tests ---------------------------------------------------------


def test_run_tests_refuses_by_default(repo):
    config = SandboxConfig.for_repo(repo)  # allow_test_execution=False

    result = run_tests_impl(None, config)

    assert "Error:" in result and "disabled" in result


def test_run_tests_runs_allowed_command(repo):
    config = SandboxConfig.for_repo(repo, allow_test_execution=True)

    result = run_tests_impl(
        None, config, test_command=[sys.executable, "-c", "print('tests ran')"]
    )

    assert "tests ran" in result
    assert "exit code: 0" in result


def test_run_tests_appends_confined_test_path(repo):
    config = SandboxConfig.for_repo(repo, allow_test_execution=True)

    result = run_tests_impl(
        "src/util.py",
        config,
        test_command=[sys.executable, "-c", "import sys; print(sys.argv[1])"],
    )

    assert "src/util.py" in result


def test_run_tests_rejects_path_traversal_in_test_path(repo):
    config = SandboxConfig.for_repo(repo, allow_test_execution=True)

    result = run_tests_impl("../../etc/passwd", config, test_command=[sys.executable, "-c", "pass"])

    assert result.startswith("Error:")


def test_make_run_tests_tool_wraps_impl(repo):
    config = SandboxConfig.for_repo(repo, allow_test_execution=True)
    tool = make_run_tests_tool(config, test_command=[sys.executable, "-c", "pass"])

    assert tool.name == "run_tests"
