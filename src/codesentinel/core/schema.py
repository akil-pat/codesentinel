"""Structured output schema for review findings.

These Pydantic models serve two purposes: they define the JSON schema
passed as `output_config.format` on the review request (so Claude's final
response is guaranteed to match this shape, not just usually match it),
and they're the same shape the eval scorer parses when comparing findings
against labeled ground truth — one definition, not two independently
drifting ones.

Verified against Anthropic's supported JSON Schema subset for structured
outputs: only object/array/string/integer/enum/$ref/$defs and
`additionalProperties: false` appear in the generated schema — no
`minLength`/numeric-range/other unsupported constraints Pydantic might
otherwise emit.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["low", "medium", "high"]


class Finding(BaseModel):
    """One specific issue found in the diff."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(description="Path to the file, relative to the repository root.")
    line: int = Field(description="1-indexed line number in the new/target version of the file.")
    severity: Severity = Field(description="How serious this issue is.")
    comment: str = Field(description="What the issue is and, where applicable, a suggested fix.")


class Review(BaseModel):
    """The complete structured output of a review."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="One or two sentence overview of the change and overall assessment."
    )
    findings: list[Finding] = Field(
        description="Specific issues found in the diff. Empty if none — do not omit this field."
    )


def review_output_format() -> dict[str, Any]:
    """Build the `output_config.format` value for a review request."""
    return {"type": "json_schema", "schema": Review.model_json_schema()}
