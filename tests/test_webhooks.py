"""Tests for codesentinel.api.webhooks.

`handle_pull_request_event` and the endpoint tests monkeypatch
GitHubClient/checkout_pull_request/run_review — no real network, no git
subprocess, no live model. GitHubClient's own httpx behavior is covered
in test_github_client.py; checkout_pull_request's real-git behavior is
covered there too. This file is about webhooks.py's own logic: signature
verification, action filtering, and orchestration.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import codesentinel.api.webhooks as webhooks_module
from codesentinel.api.app import app
from codesentinel.api.webhooks import (
    format_review_comment,
    handle_pull_request_event,
    verify_signature,
)
from codesentinel.core.agent import ReviewGenerationError, RunResult
from codesentinel.core.schema import Finding, Review

SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pr_payload(action: str = "opened", number: int = 7) -> dict:
    return {
        "action": action,
        "repository": {
            "name": "widgets",
            "owner": {"login": "acme"},
            "clone_url": "https://github.com/acme/widgets.git",
        },
        "pull_request": {"number": number, "head": {"sha": "abc123"}},
    }


# --- verify_signature ----------------------------------------------------


def test_verify_signature_accepts_correct_signature():
    body = b'{"a": 1}'
    assert verify_signature(body, _sign(body), SECRET) is True


def test_verify_signature_rejects_wrong_secret():
    body = b'{"a": 1}'
    assert verify_signature(body, _sign(body, secret="wrong"), SECRET) is False


def test_verify_signature_rejects_tampered_body():
    body = b'{"a": 1}'
    signature = _sign(body)
    assert verify_signature(b'{"a": 2}', signature, SECRET) is False


def test_verify_signature_rejects_missing_header():
    assert verify_signature(b"{}", None, SECRET) is False


def test_verify_signature_rejects_malformed_header():
    assert verify_signature(b"{}", "not-a-real-signature", SECRET) is False


# --- format_review_comment ------------------------------------------------


def test_format_review_comment_with_findings():
    review = Review(
        summary="Looks mostly fine.",
        findings=[Finding(file="a.py", line=3, severity="high", comment="bug here")],
    )

    comment = format_review_comment(review)

    assert "Looks mostly fine." in comment
    assert "**[HIGH]** `a.py:3` — bug here" in comment


def test_format_review_comment_no_findings():
    review = Review(summary="Clean change.", findings=[])

    comment = format_review_comment(review)

    assert "No issues found." in comment


# --- handle_pull_request_event ---------------------------------------------


class FakeGitHubClient:
    def __init__(self, *args, **kwargs):
        self.posted = None
        self.closed = False

    def fetch_pull_request_diff(self, owner, repo, pull_number):
        return (
            "diff --git a/src/app.py b/src/app.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def f():\n"
            "-    return 0\n"
            "+    return 1\n"
        )

    def post_issue_comment(self, owner, repo, issue_number, body):
        self.posted = (owner, repo, issue_number, body)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_github_client(monkeypatch):
    instances: list[FakeGitHubClient] = []

    def factory(*args, **kwargs):
        instance = FakeGitHubClient(*args, **kwargs)
        instances.append(instance)
        return instance

    monkeypatch.setattr(webhooks_module, "GitHubClient", factory)
    return instances


@pytest.fixture
def fake_checkout(monkeypatch, tmp_path):
    cleaned_up = []

    def fake_checkout_pull_request(clone_url, head_sha, token):
        return tmp_path

    def fake_cleanup(path):
        cleaned_up.append(path)

    monkeypatch.setattr(webhooks_module, "checkout_pull_request", fake_checkout_pull_request)
    monkeypatch.setattr(webhooks_module, "cleanup_checkout", fake_cleanup)
    return cleaned_up


def test_handle_pull_request_event_ignores_unhandled_action(fake_github_client, fake_checkout):
    handle_pull_request_event(_pr_payload(action="closed"), github_token="t")

    assert fake_github_client == []  # never even constructed a client


def test_handle_pull_request_event_posts_comment_on_success(fake_github_client, fake_checkout, monkeypatch):
    review = Review(summary="ok", findings=[])
    monkeypatch.setattr(
        webhooks_module,
        "run_review",
        lambda *a, **k: RunResult(review=review, analysis_text="x", iteration_count=1),
    )

    handle_pull_request_event(_pr_payload(), github_token="t")

    (client,) = fake_github_client
    assert client.posted == ("acme", "widgets", 7, format_review_comment(review))
    assert client.closed is True
    assert len(fake_checkout) == 1  # checkout dir was cleaned up


def test_handle_pull_request_event_never_allows_test_execution(fake_github_client, fake_checkout, monkeypatch):
    captured_config = {}

    def fake_run_review(parsed_diff, config, **kwargs):
        captured_config["config"] = config
        return RunResult(review=Review(summary="ok", findings=[]), analysis_text="x", iteration_count=1)

    monkeypatch.setattr(webhooks_module, "run_review", fake_run_review)

    handle_pull_request_event(_pr_payload(), github_token="t")

    assert captured_config["config"].allow_test_execution is False


def test_handle_pull_request_event_swallows_generation_error(fake_github_client, fake_checkout, monkeypatch):
    def raise_error(*a, **k):
        raise ReviewGenerationError("boom")

    monkeypatch.setattr(webhooks_module, "run_review", raise_error)

    handle_pull_request_event(_pr_payload(), github_token="t")  # must not raise

    (client,) = fake_github_client
    assert client.posted is None  # no comment posted on a failed review
    assert client.closed is True  # still cleaned up
    assert len(fake_checkout) == 1


def test_handle_pull_request_event_skips_review_when_diff_has_no_files(fake_github_client, fake_checkout, monkeypatch):
    class EmptyDiffClient(FakeGitHubClient):
        def fetch_pull_request_diff(self, owner, repo, pull_number):
            return ""

    monkeypatch.setattr(webhooks_module, "GitHubClient", EmptyDiffClient)
    called = []
    monkeypatch.setattr(webhooks_module, "run_review", lambda *a, **k: called.append(1))

    handle_pull_request_event(_pr_payload(), github_token="t")

    assert called == []
    assert fake_checkout == []  # never even checked out — nothing to review


# --- the actual endpoint, via TestClient ----------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    return TestClient(app)


def test_endpoint_rejects_invalid_signature(client):
    body = json.dumps(_pr_payload()).encode()

    response = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=wrong", "X-GitHub-Event": "pull_request"},
    )

    assert response.status_code == 401


def test_endpoint_rejects_missing_secret_config(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    test_client = TestClient(app)
    body = json.dumps(_pr_payload()).encode()

    response = test_client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=irrelevant", "X-GitHub-Event": "pull_request"},
    )

    assert response.status_code == 500


def test_endpoint_ignores_non_pull_request_events(client):
    body = json.dumps({"zen": "hello"}).encode()

    response = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "ping"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_endpoint_accepts_valid_pull_request_event_and_schedules_background_work(
    client, monkeypatch
):
    scheduled = []
    monkeypatch.setattr(
        webhooks_module, "_run_in_background", lambda payload, token, model: scheduled.append(payload)
    )
    body = json.dumps(_pr_payload()).encode()

    response = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "pull_request"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    # TestClient runs background tasks synchronously before returning, so
    # this reflects real behavior, not an artifact of the test harness.
    assert len(scheduled) == 1
    assert scheduled[0]["pull_request"]["number"] == 7
