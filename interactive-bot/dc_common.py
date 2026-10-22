"""Discord 同步各模块共用的常量与数据结构"""

import asyncio
import io
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import aiohttp
import discord
from telegram import error as tg_error
from telegram.constants import ReactionEmoji

from . import admin_group_id

logger = logging.getLogger("interactive-bot.discord")
# 不使用语音功能，不需要 PyNaCl
discord.VoiceClient.warn_nacl = False

NO_MENTIONS = discord.AllowedMentions.none()
WEBHOOK_NAME = "Telegram Bridge"

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API getFile 上限
TG_UPLOAD_LIMIT = 50 * 1024 * 1024  # Bot API 上传上限
TG_PHOTO_LIMIT = 10 * 1024 * 1024
MTPROTO_UPLOAD_LIMIT = 2000 * 1024 * 1024  # MTProto 机器人上传上限
COMPRESS_SOURCE_LIMIT = 1024 * 1024 * 1024  # 超过这个大小的视频不再尝试下载压缩

# Discord 错误码
DC_UNKNOWN_CHANNEL = 10003
DC_UNKNOWN_MESSAGE = 10008
DC_UNKNOWN_EMOJI = 10014
DC_UNKNOWN_WEBHOOK = 10015
DC_TOO_LARGE = 40005
DC_THREAD_ARCHIVED = 50083
DC_THREAD_LOCKED = 160005

# Telegram 允许机器人使用的表情（去掉 FE0F 变体选择符后比较）
_TG_REACTIONS = {str(e.value).replace("️", ""): str(e.value) for e in ReactionEmoji}


def tg_reaction_for(emoji):
    return _TG_REACTIONS.get((emoji or "").replace("️", ""))


def tg_message_link(group_message_id):
    """后台群消息的链接（私有群只有群成员能打开）"""
    if not group_message_id:
        return None
    internal = str(admin_group_id)
    internal = internal[4:] if internal.startswith("-100") else internal.lstrip("-")
    return f"https://t.me/c/{internal}/{group_message_id}"


class ThreadGone(Exception):
    """Discord 帖子已被删除"""


class ConversationCleared(Exception):
    """任务入队后会话被 /clear 清除，任务作废"""


def is_transient(exc):
    """可以稍后重试的错误（网络、超时、限流、服务端错误）；其余视为永久失败"""
    if isinstance(exc, (tg_error.BadRequest, tg_error.Forbidden, tg_error.InvalidToken)):
        return False
    if isinstance(exc, (tg_error.RetryAfter, tg_error.TimedOut, tg_error.NetworkError)):
        return True
    if isinstance(exc, discord.HTTPException):
        return exc.status >= 500 or exc.status == 429
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, aiohttp.ClientError)):
        return True
    name = type(exc).__name__
    # Telethon：FloodWaitError、ServerError、以及断线
    return name in ("FloodWaitError", "ServerError", "RpcCallFailError", "TimedOutError") or isinstance(exc, OSError)


@dataclass
class OutFile:
    """准备发到 Discord 的一个文件（尚未下载）"""

    # photo / video / animation / video_note / voice / audio / document /
    # sticker（静态 webp）/ sticker_tgs（Lottie 动态贴纸）/ sticker_video（webm 视频贴纸）
    kind: str
    filename: str
    label: str
    size: Optional[int] = None
    # Bot API 下载（≤20MB），返回 bytes
    fetch: Optional[Callable[[], Awaitable[bytes]]] = None
    # MTProto 下载到指定路径（不限大小），没有配置 TG_API_ID 时为 None
    fetch_to: Optional[Callable[[str], Awaitable[None]]] = None
    spoiler: bool = False
    duration: Optional[int] = None
    source: int = 0  # 对应 pairs 的下标
    link: Optional[str] = None  # TG 原消息链接，用于「未同步」说明
    fallback: Optional["OutFile"] = None  # 转换失败时的替代（动态贴纸的缩略图）


@dataclass
class OutMessage:
    """准备发到 Discord 的一条消息"""

    bodies: dict = field(default_factory=dict)  # pairs 下标 -> 该 TG 消息的正文（Markdown）
    files: list = field(default_factory=list)
    extras: list = field(default_factory=list)
    forward_from: Optional[str] = None

    @property
    def body(self):
        return "\n".join(self.bodies[k] for k in sorted(self.bodies) if self.bodies[k])


@dataclass
class Upload:
    """准备好的 Discord 附件：小文件放内存，大文件留在临时文件里直接上传"""

    filename: str
    size: int
    spoiler: bool
    label: str
    source: int = 0
    data: Optional[bytes] = None
    path: Optional[str] = None

    def to_file(self):
        fp = self.path if self.path else io.BytesIO(self.data)
        return discord.File(fp, filename=self.filename, spoiler=self.spoiler)


@dataclass
class TgItem:
    """准备发到 Telegram 的一个媒体"""

    kind: str  # photo / video / animation / audio / voice / document / copy
    data: Optional[bytes] = None
    path: Optional[str] = None  # 大文件放在临时文件里
    file_id: Optional[str] = None
    copy_from: Optional[tuple] = None  # (chat_id, message_id)，kind == "copy" 时使用
    filename: str = "file"
    spoiler: bool = False
    big: bool = False  # 超过 Bot API 上限，需要走 MTProto 上传


@dataclass
class SentRef:
    """通过 MTProto 或 copy_message 发送后只拿到消息 ID 时使用"""

    message_id: int
