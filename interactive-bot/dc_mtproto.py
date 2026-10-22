"""以机器人身份登录 MTProto（Telethon），突破 Bot API 的限制：

- 按 ID 批量读取后台群的历史消息（Bot API 无法读取历史）
- 下载超过 20MB 的文件（Bot API getFile 上限 20MB）
- 上传超过 50MB 的文件，最大 2GB（Bot API 上传上限 50MB）

只需要 TG_API_ID / TG_API_HASH（my.telegram.org 申请），不登录任何用户账号。
"""

import asyncio
from urllib.parse import urlparse

from telethon import TelegramClient, utils
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    PeerUser,
)

from . import dc_media
from .dc_common import logger


def proxy_for_telethon(proxy_url):
    if not proxy_url:
        return None
    u = urlparse(proxy_url)
    scheme = (u.scheme or "http").lower().replace("socks5h", "socks5")
    if scheme == "https":
        scheme = "http"
    proxy = {"proxy_type": scheme, "addr": u.hostname, "port": u.port or 1080}
    if u.username:
        proxy.update(username=u.username, password=u.password or "")
    return proxy


class MtprotoClient:
    def __init__(self, *, api_id, api_hash, bot_token, session, group_id, proxy=None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.session = session
        self.group_id = group_id
        self.proxy = proxy_for_telethon(proxy)
        self.bot_id = None
        self._client = None
        self._group = None
        self._lock = asyncio.Lock()

    async def client(self):
        async with self._lock:
            if self._client and self._client.is_connected():
                return self._client
            client = TelegramClient(
                self.session,
                self.api_id,
                self.api_hash,
                proxy=self.proxy,
                receive_updates=False,
                flood_sleep_threshold=300,
            )
            await client.start(bot_token=self.bot_token)
            self.bot_id = (await client.get_me()).id
            real_id, peer_type = utils.resolve_id(self.group_id)
            self._group = await client.get_input_entity(peer_type(real_id))
            self._client = client
            logger.info("MTProto 已连接")
            return client

    async def close(self):
        if self._client:
            await self._client.disconnect()

    async def get_messages(self, ids):
        """读取后台群消息，不存在的位置为 None"""
        client = await self.client()
        return await client.get_messages(self._group, ids=list(ids))

    async def download(self, message, path, thumb=None):
        client = await self.client()
        result = await client.download_media(message, file=path, thumb=thumb)
        if not result:
            raise RuntimeError("消息中没有可下载的文件")

    async def download_group_media(self, group_message_id, path, thumb=None):
        (message,) = await self.get_messages([group_message_id])
        if message is None or not message.media:
            raise RuntimeError(f"后台群消息 {group_message_id} 不存在或没有文件")
        await self.download(message, path, thumb=thumb)

    async def send_file(self, chat_id, path, *, kind, filename, reply_to=None, caption=None):
        """发送大文件，返回消息 ID（私聊里 MTProto 与 Bot API 的消息 ID 一致）"""
        client = await self.client()
        # 机器人给私聊过它的用户发消息时，access_hash 可以为 0
        entity = await client.get_input_entity(PeerUser(chat_id))
        attributes = [DocumentAttributeFilename(filename)]
        if kind == "video":
            info = await dc_media.probe_video(path)
            if info:
                duration, width, height = info
                attributes.append(
                    DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)
                )
        elif kind == "audio":
            duration = await dc_media.probe_duration(path)
            attributes.append(DocumentAttributeAudio(duration=int(duration or 0)))
        message = await client.send_file(
            entity,
            path,
            caption=caption,
            reply_to=reply_to,
            force_document=kind not in ("video", "audio"),
            supports_streaming=kind == "video",
            attributes=attributes,
        )
        return message.id
