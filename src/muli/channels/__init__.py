"""可选渠道；默认关闭，启用后仅映射明确绑定的 owner。"""

from .manager import ChannelManager
from .types import (
    ChannelError,
    ChannelProfile,
    ChannelsConfig,
    FileContent,
    IncomingMessage,
    RemoteFile,
)

__all__ = [
    "ChannelError",
    "ChannelManager",
    "ChannelProfile",
    "ChannelsConfig",
    "FileContent",
    "IncomingMessage",
    "RemoteFile",
]
