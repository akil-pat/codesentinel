"""Tests for the security boundary in codesentinel.core.sandbox.

These are the tests that matter most in this codebase: every case here is
a specific adversarial scenario the design review flagged (path traversal,
symlink escape, unsandboxed execution, ReDoS), not a generic smoke test.
"""

from __future__ import annotations

import re
import signal
import sys
import time

import pytest

from codesentinel.core.sandbox import (
    RegexTimeout,
    SandboxConfig,
    SandboxViolation,
    resolve_within_root,
    run_sandboxed,
    safe_regex_search,
)

requires_sigalrm = pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"), reason="SIGALRM-based timeout is Unix-only"
)


# --- resolve_within_root -----------------------------------------------


def test_resolve_within_root_allows_normal_relative_path(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")

    resolved = resolve_within_root("src/app.py", tmp_path)

    assert resolved == (tmp_path / "src" / "app.py").resolve()


def test_resolve_within_root_rejects_parent_traversal(tmp_path):
    with pytest.raises(SandboxViolation):
        resolve_within_root("../../etc/passwd", tmp_path)


def test_resolve_within_root_rejects_absolute_path_outside_root(tmp_path):
    with pytest.raises(SandboxViolation):
        resolve_within_root("/etc/passwd", tmp_path)


def test_resolve_within_root_allows_absolute_path_inside_root(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("hi\n")

    resolved = resolve_within_root(str(target), tmp_path)

    assert resolved == target.resolve()


def test_resolve_within_root_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("nope\n")

    link = tmp_path / "escape"
    link.symlink_to(outside)

    with pytest.raises(SandboxViolation):
        resolve_within_root("escape/secret.txt", tmp_path)


def test_resolve_within_root_rejects_nonexistent_root(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_within_root("x", tmp_path / "does-not-exist")


# --- safe_regex_search ---------------------------------------------------


def test_safe_regex_search_returns_matches_for_normal_pattern():
    matches = safe_regex_search(re.compile(r"\bfoo\b"), "foo bar foo baz", timeout=1.0)

    assert [m.group() for m in matches] == ["foo", "foo"]


@requires_sigalrm
def test_safe_regex_search_times_out_on_catastrophic_backtracking():
    # Classic ReDoS shape: nested unbounded quantifiers followed by a
    # forced-failure suffix. Without the timeout this takes seconds to
    # minutes depending on input length; the timeout must fire well before
    # that on a short budget.
    evil_pattern = re.compile(r"^(a+)+$")
    evil_input = "a" * 30 + "!"

    started = time.monotonic()
    with pytest.raises(RegexTimeout):
        safe_regex_search(evil_pattern, evil_input, timeout=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, "timeout did not actually bound the regex execution"


@requires_sigalrm
def test_safe_regex_search_cleans_up_alarm_after_success():
    # A prior bug here would leave a pending SIGALRM armed after a
    # successful (non-timing-out) call, firing spuriously later.
    safe_regex_search(re.compile(r"foo"), "foo", timeout=0.05)
    time.sleep(0.2)  # would-be alarm fires here if cleanup is broken
    # No exception means no leaked alarm.


# --- run_sandboxed ---------------------------------------------------------


def test_run_sandboxed_refuses_execution_by_default(tmp_path):
    config = SandboxConfig.for_repo(tmp_path)  # allow_test_execution=False

    with pytest.raises(SandboxViolation, match="disabled"):
        run_sandboxed([sys.executable, "-c", "print('should not run')"], config=config)


def test_run_sandboxed_runs_allowed_command(tmp_path):
    config = SandboxConfig.for_repo(tmp_path, allow_test_execution=True)

    result = run_sandboxed([sys.executable, "-c", "print('ok')"], config=config)

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_run_sandboxed_confines_cwd_to_repo_root(tmp_path):
    config = SandboxConfig.for_repo(tmp_path, allow_test_execution=True)
    outside = tmp_path.parent / f"outside-cwd-{tmp_path.name}"
    outside.mkdir()

    with pytest.raises(SandboxViolation):
        run_sandboxed([sys.executable, "-c", "pass"], config=config, cwd=outside)


def test_run_sandboxed_enforces_timeout(tmp_path):
    config = SandboxConfig.for_repo(
        tmp_path, allow_test_execution=True, test_timeout_seconds=0.2
    )

    with pytest.raises(SandboxViolation, match="timeout"):
        run_sandboxed(
            [sys.executable, "-c", "import time; time.sleep(5)"], config=config
        )


def test_run_sandboxed_strips_ambient_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("SOME_APP_SECRET", "also-should-not-leak")
    config = SandboxConfig.for_repo(tmp_path, allow_test_execution=True)

    result = run_sandboxed(
        [sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'))"],
        config=config,
    )

    assert result.stdout.strip() == "ABSENT"


def test_run_sandboxed_never_uses_a_shell(tmp_path):
    # If argv were ever passed through a shell, this would create the file;
    # with shell=False it's just a literal (nonexistent) argv, which the
    # interpreter reports as a syntax/usage error rather than executing.
    config = SandboxConfig.for_repo(tmp_path, allow_test_execution=True)
    marker = tmp_path / "pwned"

    run_sandboxed(
        [sys.executable, "-c", "print('hi')", ";", "touch", str(marker)],
        config=config,
    )

    assert not marker.exists()
