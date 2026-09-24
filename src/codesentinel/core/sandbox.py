"""Security boundary for tool execution.

Every tool that touches the filesystem or spawns a process from
model-supplied input (file paths, regex patterns, test targets — all
ultimately derived from untrusted diff content) MUST route through this
module before doing so. Nothing here provides full OS-level isolation on
its own; see the limitations noted on `run_sandboxed`.

Provides:
  - `resolve_within_root`: path confinement. Rejects `..` traversal,
    symlink escapes, and absolute paths outside the repo root.
  - `safe_regex_search`: a wall-clock timeout around regex matching, to
    guard against catastrophic backtracking (ReDoS) from a model- or
    diff-supplied pattern.
  - `SandboxConfig` / `run_sandboxed`: a restricted subprocess runner for
    `run_tests` — confined working directory, an allowlisted environment
    (no inherited credentials), a hard timeout, and a runtime guard that
    refuses to execute at all unless the caller has explicitly opted a
    given repo into test execution.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from re import Pattern


class SandboxViolation(Exception):
    """Raised when an operation would escape its sandbox (path, execution
    policy, or timeout)."""


class RegexTimeout(Exception):
    """Raised when a regex search exceeds its time budget — treat as a
    likely-ReDoS pattern, not a retryable error."""


def resolve_within_root(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> Path:
    """Resolve `path` against `root` and confine it inside `root`.

    `path` may be relative (resolved against `root`) or absolute (checked
    directly). Either way, the *resolved* path — after following symlinks —
    must land inside `root`, or this raises `SandboxViolation`. This is the
    single check every filesystem-touching tool must call before opening,
    reading, or executing anything the model named.

    `root` must already exist. `path` need not (e.g. before a write), so
    resolution uses `strict=False` on the candidate but `strict=True` on
    `root` — an unresolvable root means the sandbox itself is misconfigured
    and should fail loudly, not silently no-op.
    """
    root_resolved = Path(root).resolve(strict=True)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve(strict=False)

    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise SandboxViolation(
            f"path {str(path)!r} resolves to {resolved}, which escapes the "
            f"sandbox root {root_resolved}"
        ) from None
    return resolved


@contextlib.contextmanager
def _time_limit(seconds: float):
    """Unix-only wall-clock timeout via SIGALRM.

    CPython's `re` engine periodically checks for pending signals during
    matching (it isn't one opaque uninterruptible C call), so SIGALRM
    reliably interrupts catastrophic backtracking in practice — this is
    the standard technique for bounding regex execution in Python, not a
    guess. It is NOT thread-safe (signal handlers are process-global) and
    only fires on the main thread; callers running tools from a worker
    thread need a different mechanism (e.g. a subprocess with its own
    timeout). Not available on Windows — falls back to no enforcement
    there, so bound input size upstream on that platform instead. Not
    reentrant/nestable — a nested call overwrites the outer timer; this
    module never nests calls, so that's not exercised here.
    """
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handler(signum: int, frame: object) -> None:
        raise RegexTimeout(f"operation exceeded {seconds}s time limit")

    previous_handler = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def safe_regex_search(pattern: Pattern[str], text: str, *, timeout: float = 2.0) -> list:
    """Run `pattern.finditer(text)` under a wall-clock timeout.

    Raises `RegexTimeout` instead of hanging on a catastrophic-backtracking
    pattern. This is a last line of defense — prefer rejecting obviously
    dangerous patterns (nested quantifiers over unbounded input) before
    ever reaching here, but don't rely on that alone: pattern danger is
    genuinely hard to detect statically, and the input is attacker-
    influenceable.
    """
    with _time_limit(timeout):
        return list(pattern.finditer(text))


@dataclass(frozen=True)
class SandboxConfig:
    """Per-run sandbox policy. Construct once per review and thread it
    through every tool call — never re-derive these settings ad hoc inside
    a tool, or a future tool can silently bypass the policy.
    """

    repo_root: Path
    allow_test_execution: bool = False
    test_timeout_seconds: float = 60.0
    regex_timeout_seconds: float = 2.0

    @classmethod
    def for_repo(
        cls,
        repo_root: str | os.PathLike[str],
        *,
        allow_test_execution: bool = False,
        test_timeout_seconds: float = 60.0,
        regex_timeout_seconds: float = 2.0,
    ) -> SandboxConfig:
        return cls(
            repo_root=Path(repo_root).resolve(strict=True),
            allow_test_execution=allow_test_execution,
            test_timeout_seconds=test_timeout_seconds,
            regex_timeout_seconds=regex_timeout_seconds,
        )


# Minimal allowlist for a sandboxed subprocess's environment. Deliberately
# an allowlist, not a denylist: a denylist of "known secret-shaped" prefixes
# would still leak arbitrary app-specific secrets (DATABASE_URL, STRIPE_KEY,
# etc.) that happen to be sitting in the parent process's environment. The
# target repo's own test config, if any, is not this tool's problem to
# satisfy — a test that needs more than this either fails loudly (fine) or
# needs a maintainer-provided environment, not an inherited one.
_ENV_ALLOWLIST = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONIOENCODING", "VIRTUAL_ENV"}
)


def _sandboxed_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


def run_sandboxed(
    argv: list[str],
    *,
    config: SandboxConfig,
    cwd: str | os.PathLike[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `argv` as a subprocess under `config`'s sandbox policy.

    Enforces, in order:
      1. `config.allow_test_execution` must be True. Off by default — this
         is a runtime guard, not a README warning. Reviewing an arbitrary
         repo means running that repo's code (fixtures, `conftest.py`,
         imports); the caller must explicitly opt a given repo into that
         (e.g. a `--allow-tests` CLI flag) rather than get it for free.
      2. `cwd` (or `config.repo_root` if unset) is confined inside
         `config.repo_root` via `resolve_within_root`.
      3. The subprocess environment is reduced to `_sandboxed_env()` — no
         inherited API keys, tokens, or cloud credentials.
      4. `shell=False` always — `argv` is an explicit list the OS execs
         directly, never a string a shell would interpret. Never build
         `argv` by string-formatting model output into a command line.
      5. A hard wall-clock timeout (`config.test_timeout_seconds`), raised
         as `SandboxViolation` rather than left as a bare
         `TimeoutExpired` so every sandbox failure is one exception type.

    Limitations — this does NOT provide full OS-level isolation. There is
    no network namespace and no filesystem isolation beyond `cwd`
    confinement; a subprocess on the host can still, in principle, reach
    the network or read files elsewhere on disk if the code it runs tries
    hard enough. For the FastAPI webhook path (reviewing PRs from
    arbitrary/untrusted repos), this MUST run inside the container
    described in `Dockerfile`, whose own network and filesystem
    restrictions provide the isolation this function alone cannot. Treat
    this function as the policy layer, not the isolation boundary, when
    the target repo isn't fully trusted.
    """
    if not config.allow_test_execution:
        raise SandboxViolation(
            "test execution is disabled for this run (SandboxConfig."
            "allow_test_execution=False) — only enable it for repos you "
            "trust; running tests means running that repo's code"
        )

    work_dir = resolve_within_root(
        cwd if cwd is not None else config.repo_root, config.repo_root
    )

    try:
        return subprocess.run(
            argv,
            cwd=work_dir,
            env=_sandboxed_env(),
            shell=False,
            capture_output=True,
            text=True,
            timeout=config.test_timeout_seconds,
            check=False,  # a failing test run is expected output, not an exception
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxViolation(
            f"command {argv!r} exceeded the {config.test_timeout_seconds}s "
            "sandbox timeout"
        ) from exc
