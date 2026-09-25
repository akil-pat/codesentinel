"""Thin GitHub REST API client plus a PR-checkout helper for the webhook
service. Uses `httpx` directly rather than a full GitHub SDK — the
surface needed here (fetch a PR diff, post a comment, check out a PR's
head commit) is small enough that a dependency isn't worth it.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

GITHUB_API_BASE = "https://api.github.com"


class GitHubClient:
    """Wraps the handful of GitHub REST calls the webhook service needs.

    `transport` is injectable (an `httpx.MockTransport` in tests) rather
    than accepting a whole pre-built `httpx.Client` — injecting at the
    transport level means this class always sets its own auth headers on
    every request, in tests and in production alike, instead of tests
    silently bypassing header setup by supplying their own client.
    """

    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=GITHUB_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport=transport,
            timeout=30.0,
        )

    def fetch_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        """Fetch a PR's unified diff via GitHub's diff media type."""
        response = self._http.get(
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        response.raise_for_status()
        return response.text

    def post_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> None:
        """Post a comment on a PR — PRs are issues in the GitHub API, so
        this is the issue-comments endpoint, not a formal PR review.
        A single summary comment (not per-line inline review comments,
        which need diff-position math significantly more involved than
        this project's scope) is the deliberate choice here.
        """
        response = self._http.post(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        response.raise_for_status()

    def close(self) -> None:
        self._http.close()


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _run_git(args: list[str], *, cwd: Path | None = None, timeout: float, redact_secret: str = "") -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} failed (exit {result.returncode}): "
            f"{_redact(result.stderr.strip(), redact_secret)}"
        )


def checkout_pull_request(clone_url: str, head_sha: str, token: str, *, timeout: float = 60.0) -> Path:
    """Shallow-clone `clone_url` and check out `head_sha` into a fresh
    temp directory. The caller owns the returned directory and must clean
    it up with `cleanup_checkout` — this function doesn't register any
    automatic cleanup, since the directory needs to survive across the
    review that follows.

    `token` is embedded in the clone URL using GitHub's documented
    tokened-clone form (`https://x-access-token:<token>@host/...`) — the
    same approach GitHub's own `actions/checkout` uses. It's redacted
    from any error this function raises, but note that doesn't protect
    against the token being visible to another user on this host via
    `ps` while the git subprocess is briefly running; a credential-helper
    or GIT_ASKPASS indirection would close that gap but is disproportionate
    to this project's scope, so the residual risk is accepted and
    documented here rather than silently ignored.

    Only ever run `run_tests` against a directory from here with
    `allow_test_execution=False` unless a human has explicitly reviewed
    the PR — this checks out and makes readable arbitrary contributor-
    submitted code.
    """
    dest = Path(tempfile.mkdtemp(prefix="codesentinel-pr-"))
    authed_url = _with_token(clone_url, token)
    try:
        _run_git(
            ["clone", "--no-checkout", "--filter=blob:none", authed_url, str(dest)],
            timeout=timeout,
            redact_secret=token,
        )
        _run_git(["fetch", "--depth", "1", "origin", head_sha], cwd=dest, timeout=timeout, redact_secret=token)
        _run_git(["checkout", "--detach", head_sha], cwd=dest, timeout=timeout)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def cleanup_checkout(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _with_token(clone_url: str, token: str) -> str:
    if not token or "://" not in clone_url:
        return clone_url
    scheme, rest = clone_url.split("://", 1)
    return f"{scheme}://x-access-token:{token}@{rest}"
