"""频道停止后不得继续暴露已停止的主频道。"""

from app import ChannelManager


def test_stop_primary_clears_reference():
    manager = ChannelManager()
    channel = object()
    manager._channels["web"] = channel
    manager._primary_channel = channel
    manager.stop("web")
    assert manager.channel is None


def test_stop_all_clears_primary_reference():
    manager = ChannelManager()
    channel = object()
    manager._channels["web"] = channel
    manager._primary_channel = channel
    manager.stop()
    assert manager.channel is None


def test_stop_other_channel_preserves_primary():
    manager = ChannelManager()
    primary = object()
    manager._channels.update(web=primary, other=object())
    manager._primary_channel = primary
    manager.stop("other")
    assert manager.channel is primary
