"""GitHub webhook endpoint.

Must verify the X-Hub-Signature-256 header against GITHUB_WEBHOOK_SECRET
before processing any payload — an unverified webhook endpoint lets anyone
trigger a review run (and, worse, feed it attacker-chosen diff content).
Implementation TBD.
"""
