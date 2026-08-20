"""Dashboard cursor codec tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from llm_router.dashboard.cursor import decode_cursor, encode_cursor
from llm_router.dashboard.models import RequestCursor


def test_cursor_round_trip_is_versioned_and_typed() -> None:
    """Round-trip preserves the only two keyset coordinates."""

    cursor = RequestCursor(datetime(2026, 8, 19, tzinfo=UTC), uuid4())
    encoded = encode_cursor(cursor)
    decoded = decode_cursor(encoded)
    assert decoded == cursor
    assert "=" not in encoded


@pytest.mark.parametrize("value", ["", "not-base64", "eyJ2IjoyfQ"])
def test_invalid_cursor_is_rejected(value: str) -> None:
    """Malformed or unsupported cursor versions fail before SQL."""

    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor(value)
