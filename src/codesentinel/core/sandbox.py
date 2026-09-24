"""Security boundary for tool execution.

Enforces, before any tool touches the filesystem or spawns a process:
  - path confinement: every model-supplied path is resolved to its canonical
    form and checked to be relative to the repo root (reject `..`, symlink
    escapes, absolute paths outside the root).
  - subprocess sandboxing for `run_tests`: no network access, restricted
    working directory, timeout.
  - regex safety for `grep_repo`: execution timeout to avoid catastrophic
    backtracking (ReDoS) on model- or diff-supplied patterns.

Implementation TBD — every tool in core/tools/ must route through here
before touching disk or spawning a process.
"""
