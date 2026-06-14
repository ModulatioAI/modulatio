"""Regression tests for r2 audit findings in provider_catalog.parse_models.

Covers:
  - parse_models must not crash on non-list-of-dict /models payloads
    (error envelopes, scalars, malformed feed rows).
  - non-integer created/context_length must not abort the whole catalog.
"""

from modulatio.provider_catalog import get_provider, parse_models


def _provider():
    p = get_provider("openrouter")
    assert p is not None
    return p


def test_error_envelope_dict_does_not_crash():
    # An error body returned with HTTP 200 has no data/models key -> raw becomes
    # the dict itself; iterating its string keys must not raise AttributeError.
    payload = {"error": {"message": "invalid key", "code": 401}}
    assert parse_models(_provider(), payload) == []


def test_scalar_payload_does_not_crash():
    assert parse_models(_provider(), 42) == []
    assert parse_models(_provider(), "boom") == []
    assert parse_models(_provider(), None) == []


def test_malformed_non_dict_rows_are_skipped():
    payload = {"data": ["just-a-string", 7, None, {"id": "good-model"}]}
    out = parse_models(_provider(), payload)
    assert [m.id for m in out] == ["good-model"]


def test_non_integer_fields_do_not_abort_catalog():
    payload = {
        "data": [
            {"id": "m1", "created": "2024-01-01", "context_length": "128k"},
            {"id": "m2", "created": 1700000000, "context_length": 8192},
        ]
    }
    out = parse_models(_provider(), payload)
    assert [m.id for m in out] == ["m1", "m2"]
    # bad row coerces to None instead of dropping the model or raising.
    assert out[0].created is None
    assert out[0].context_length is None
    assert out[1].created == 1700000000
    assert out[1].context_length == 8192


def test_numeric_string_fields_still_coerce():
    payload = {"data": [{"id": "m", "created": "1700000000", "context_length": "8192"}]}
    out = parse_models(_provider(), payload)
    assert out[0].created == 1700000000
    assert out[0].context_length == 8192


def test_bool_field_not_treated_as_int():
    payload = {"data": [{"id": "m", "context_length": True}]}
    out = parse_models(_provider(), payload)
    assert out[0].context_length is None
