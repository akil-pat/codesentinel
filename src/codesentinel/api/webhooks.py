"""GitHub webhook endpoint.

Verifies X-Hub-Signature-256 against GITHUB_WEBHOOK_SECRET before
processing anything — an unverified webhook endpoint lets anyone trigger
a review run against a repo/commit of their choosing and feed it
attacker-chosen diff content.

Runs the actual review in a FastAPI background task rather than inline:
GitHub expects a webhook response quickly, and a real agent review
(multiple tool-use turns plus a restate call) can easily take longer than
that. The endpoint verifies and dispatches, then returns immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from codesentinel.api.github_client import GitHubClient, checkout_pull_request, cleanup_checkout
from codesentinel.core.agent import DEFAULT_MODEL, ReviewGenerationError, run_review
from codesentinel.core.diff import parse_diff
from codesentinel.core.sandbox import SandboxConfig
from codesentinel.core.schema import Review

logger = logging.getLogger(__name__)

router = APIRouter()

# Reviewing on every push to an open PR (synchronize) as well as on open
# is the useful case; ignore everything else (closed, labeled, assigned,
# ...) rather than trying to react to every PR lifecycle action.
HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


def verify_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Constant-time verification of GitHub's X-Hub-Signature-256 header.

    Returns False rather than raising on any malformed/missing input, so
    the endpoint can uniformly turn any falsy result into a 401 — a
    missing or malformed header isn't meaningfully different from a wrong
    one from a security standpoint, and shouldn't get different handling.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def format_review_comment(review: Review) -> str:
    """Render a Review as a GitHub-flavored Markdown PR comment."""
    lines = ["## codesentinel review", "", review.summary, ""]
    if not review.findings:
        lines.append("No issues found.")
    else:
        for finding in review.findings:
            lines.append(
                f"- **[{finding.severity.upper()}]** `{finding.file}:{finding.line}` — {finding.comment}"
            )
    return "\n".join(lines)


def handle_pull_request_event(payload: dict, *, github_token: str, model: str = DEFAULT_MODEL) -> None:
    """Process one `pull_request` webhook event: fetch the diff, check
    out the PR head, run the review, post the result as a comment.

    Exceptions from a failed review (ReviewGenerationError) are logged
    and swallowed here, not re-raised — a review that couldn't be
    produced for one PR shouldn't be treated as a crash of the service.
    Any other exception (network, checkout failure, ...) propagates to
    the caller, which is expected to be the background-task wrapper that
    logs it without ever surfacing it back to GitHub (the HTTP response
    was already sent by the time this runs).
    """
    action = payload.get("action")
    if action not in HANDLED_ACTIONS:
        return

    repo = payload["repository"]
    pr = payload["pull_request"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    pr_number = pr["number"]
    clone_url = repo["clone_url"]
    head_sha = pr["head"]["sha"]

    client = GitHubClient(token=github_token)
    checkout_dir = None
    try:
        diff_text = client.fetch_pull_request_diff(owner, repo_name, pr_number)
        parsed_diff = parse_diff(diff_text)
        if not parsed_diff.files:
            return

        checkout_dir = checkout_pull_request(clone_url, head_sha, github_token)
        # Never auto-run a PR's own tests from a webhook — this repo could
        # be anyone's fork submitting anything. See checkout_pull_request's
        # docstring.
        config = SandboxConfig.for_repo(checkout_dir, allow_test_execution=False)

        result = run_review(parsed_diff, config, model=model)
        client.post_issue_comment(owner, repo_name, pr_number, format_review_comment(result.review))
    except ReviewGenerationError as exc:
        logger.warning("review generation failed for %s/%s#%d: %s", owner, repo_name, pr_number, exc)
    finally:
        client.close()
        if checkout_dir is not None:
            cleanup_checkout(checkout_dir)


def _run_in_background(payload: dict, github_token: str, model: str) -> None:
    try:
        handle_pull_request_event(payload, github_token=github_token, model=model)
    except Exception:
        logger.exception("unhandled error processing webhook payload in background task")


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, str]:
    body = await request.body()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_SECRET is not configured")

    if not verify_signature(body, x_hub_signature_256, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"unhandled event type {x_github_event!r}"}

    payload = json.loads(body)
    github_token = os.environ.get("GITHUB_TOKEN", "")
    model = os.environ.get("CODESENTINEL_MODEL", DEFAULT_MODEL)

    background_tasks.add_task(_run_in_background, payload, github_token, model)
    return {"status": "accepted"}
