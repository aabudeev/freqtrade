# pragma pylint: disable=missing-docstring
from __future__ import annotations

from freqtrade.signals.channel_smoke import resolve_channel_peer_id


def test_resolve_channel_peer_id_positive():
    assert resolve_channel_peer_id("1566432615") == -1001566432615


def test_resolve_channel_peer_id_already_full():
    assert resolve_channel_peer_id("-1001566432615") == -1001566432615
