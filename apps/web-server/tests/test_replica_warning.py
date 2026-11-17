"""#2059: warn when the chart reaches more than one control-plane replica."""

import logging

import pytest
from server.config import warn_if_multi_replica


@pytest.mark.parametrize(
    ("value", "warns"),
    [
        ("2", True),
        ("4", True),
        ("1", False),
        ("", False),
        ("two", False),
        (None, False),
    ],
)
def test_warns_only_above_one_replica(monkeypatch, caplog, value, warns):
    if value is None:
        monkeypatch.delenv("AIFACTORY_REPLICAS", raising=False)
    else:
        monkeypatch.setenv("AIFACTORY_REPLICAS", value)
    with caplog.at_level(logging.WARNING, logger="server.config"):
        warn_if_multi_replica()
    assert ("revert-PR idempotency" in caplog.text) is warns
