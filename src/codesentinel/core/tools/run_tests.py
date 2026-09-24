"""run_tests tool.

Executes exclusively through `core.sandbox.run_sandboxed`, never a raw
`subprocess` call — the test target and the repository's own code are both
attacker-influenceable (the repo under review may itself be untrusted, and
its content shapes what the model asks this tool to run). `run_sandboxed`
refuses to execute at all unless `config.allow_test_execution` was
explicitly set True by the caller for this repo; that refusal surfaces
here as a plain error string returned to the model, not an exception that
aborts the whole review.
"""

from __future__ import annotations

import sys

from anthropic import beta_tool

from codesentinel.core.sandbox import (
    SandboxConfig,
    SandboxViolation,
    resolve_within_root,
    run_sandboxed,
)

DEFAULT_TEST_COMMAND = [sys.executable, "-m", "pytest", "-q"]


def run_tests_impl(
    test_path: str | None,
    config: SandboxConfig,
    test_command: list[str] | None = None,
) -> str:
    """Pure implementation, independent of the `@beta_tool` wrapper."""
    argv = list(test_command or DEFAULT_TEST_COMMAND)

    if test_path is not None:
        try:
            resolved = resolve_within_root(test_path, config.repo_root)
        except SandboxViolation as exc:
            return f"Error: {exc}"
        argv.append(str(resolved.relative_to(config.repo_root)))

    try:
        result = run_sandboxed(argv, config=config)
    except SandboxViolation as exc:
        return f"Error: {exc}"

    output = f"$ {' '.join(argv)}\nexit code: {result.returncode}\n\n{result.stdout}"
    if result.stderr:
        output += f"\n--- stderr ---\n{result.stderr}"
    return output


def make_run_tests_tool(config: SandboxConfig, test_command: list[str] | None = None):
    """Build a run_tests tool bound to `config`. Call once per review.

    `test_command` lets a caller override the default `pytest -q` (e.g. for
    a repo that uses a different runner) — set at construction time, not
    something the model can change per call.
    """

    @beta_tool
    def run_tests(test_path: str | None = None) -> str:
        """Run the test suite (or one test file/directory) and report
        pass/fail output, so you can check whether a change is actually
        covered before recommending it, or confirm a fix doesn't break
        existing tests.

        Args:
            test_path: Optional path, relative to the repository root, to
                a specific test file or directory. If omitted, runs the
                default test command for the whole repository.
        """
        return run_tests_impl(test_path, config, test_command)

    return run_tests
