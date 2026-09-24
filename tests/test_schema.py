"""Tests for core.schema — mainly that the generated JSON schema actually
stays inside Anthropic's supported structured-outputs subset, since
Pydantic will happily emit constraints (minLength, numeric ranges, etc.)
that the API silently doesn't enforce or outright rejects.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from codesentinel.core.schema import Finding, Review, review_output_format


def _walk_schema_nodes(schema: dict):
    """Yield every dict node in a JSON schema, including nested $defs."""
    yield schema
    for value in schema.values():
        if isinstance(value, dict):
            yield from _walk_schema_nodes(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from _walk_schema_nodes(item)


UNSUPPORTED_KEYS = {
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "multipleOf",
    "exclusiveMinimum",
    "exclusiveMaximum",
}


def test_review_output_format_shape():
    fmt = review_output_format()

    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["title"] == "Review"


def test_review_schema_stays_in_supported_subset():
    schema = review_output_format()["schema"]

    for node in _walk_schema_nodes(schema):
        assert not (UNSUPPORTED_KEYS & node.keys()), f"unsupported constraint in {node}"
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"missing additionalProperties: false in {node}"


def test_finding_requires_all_fields():
    with pytest.raises(ValidationError):
        Finding(file="a.py", line=1, severity="high")  # missing comment


def test_finding_rejects_invalid_severity():
    with pytest.raises(ValidationError):
        Finding(file="a.py", line=1, severity="critical", comment="x")


def test_review_findings_field_is_required_not_defaulted():
    with pytest.raises(ValidationError):
        Review(summary="looks fine")  # findings must be present, even if empty


def test_review_accepts_empty_findings_list():
    review = Review(summary="No issues found.", findings=[])

    assert review.findings == []
