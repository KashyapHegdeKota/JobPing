"""Operational reporting is bounded and cancellation stops its owned loop."""

import asyncio
from unittest.mock import patch

import pytest
from app.request_limits.diagnostics import report_limits

from tests.unit.request_limit_fakes import limiter


async def test_reporter_has_no_identity_fields() -> None:
    value = limiter()
    await value.check("public", "ip", "private-address")
    with (
        patch(
            "app.request_limits.diagnostics.asyncio.sleep",
            side_effect=[None, asyncio.CancelledError],
        ),
        patch("app.request_limits.diagnostics.logger.info") as log,
    ):
        with pytest.raises(asyncio.CancelledError):
            await report_limits(value)
    assert log.call_count == 1
    assert "private-address" not in str(log.call_args)
    assert log.call_args.args[2] == {"public.allowed": 1}
