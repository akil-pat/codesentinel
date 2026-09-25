"""Tests for codesentinel.api.github_client.

API-call tests use httpx.MockTransport — no real network. The
checkout_pull_request tests use a REAL local git repo (created via `git
init` in a temp dir, used as the "remote" via a file:// path) so the
actual clone/fetch/checkout subprocess calls are genuinely exercised,
not mocked away — this is the same lesson as core.diff's tests: a git
operation is exactly the kind of thing worth testing against the real
tool rather than a guess at its behavior.
"""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from codesentinel.api.github_client import (
    GitHubClient,
    checkout_pull_request,
    cleanup_checkout,
)


def _git(args: list[str], cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def remote_repo(tmp_path):
    """A real local git repo with two commits, usable as a clone source."""
    repo = tmp_path / "remote"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "t@t.com"], cwd=repo)
    _git(["config", "user.name", "t"], cwd=repo)
    (repo / "a.txt").write_text("first\n")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-q", "-m", "first"], cwd=repo)
    first_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "a.txt").write_text("second\n")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-q", "-m", "second"], cwd=repo)
    second_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    return repo, first_sha, second_sha


# --- GitHubClient: API calls via httpx.MockTransport ------------------


def test_fetch_pull_request_diff_returns_body_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/acme/widgets/pulls/42"
        assert request.headers["accept"] == "application/vnd.github.v3.diff"
        assert request.headers["authorization"] == "Bearer tok123"
        return httpx.Response(200, text="diff --git a/x b/x\n")

    client = GitHubClient(token="tok123", transport=httpx.MockTransport(handler))

    diff = client.fetch_pull_request_diff("acme", "widgets", 42)

    assert diff == "diff --git a/x b/x\n"


def test_fetch_pull_request_diff_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = GitHubClient(token="tok123", transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_pull_request_diff("acme", "widgets", 42)


def test_post_issue_comment_sends_body_json():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    client = GitHubClient(token="tok123", transport=httpx.MockTransport(handler))

    client.post_issue_comment("acme", "widgets", 42, "hello review")

    assert captured["method"] == "POST"
    assert captured["path"] == "/repos/acme/widgets/issues/42/comments"
    assert captured["json"] == {"body": "hello review"}


# --- checkout_pull_request: against a real local git repo -------------


def test_checkout_pull_request_checks_out_the_correct_commit(remote_repo):
    repo, first_sha, _second_sha = remote_repo
    clone_url = f"file://{repo}"

    dest = checkout_pull_request(clone_url, first_sha, token="")
    try:
        assert (dest / "a.txt").read_text() == "first\n"
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert head == first_sha
    finally:
        cleanup_checkout(dest)

    assert not dest.exists()


def test_checkout_pull_request_cleans_up_on_failure(remote_repo):
    repo, _first_sha, _second_sha = remote_repo
    clone_url = f"file://{repo}"

    with pytest.raises(RuntimeError):
        checkout_pull_request(clone_url, "0" * 40, token="")  # sha that doesn't exist

    # Can't assert the exact path (checkout_pull_request generates its own
    # tempdir internally), but we can assert no codesentinel-pr-* tempdirs
    # were left behind.
    import glob
    import tempfile

    leftover = glob.glob(f"{tempfile.gettempdir()}/codesentinel-pr-*")
    assert leftover == [], f"checkout_pull_request left temp dirs behind: {leftover}"


def test_checkout_pull_request_redacts_token_from_errors(remote_repo):
    repo, _first_sha, _second_sha = remote_repo
    clone_url = f"file://{repo}"
    secret_token = "sekrit-token-value"

    with pytest.raises(RuntimeError) as exc_info:
        checkout_pull_request(clone_url, "0" * 40, token=secret_token)

    assert secret_token not in str(exc_info.value)
