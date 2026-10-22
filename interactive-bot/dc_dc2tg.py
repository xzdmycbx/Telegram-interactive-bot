"""Discord → Telegram：Discord 管理员在帖子里的发言发给用户，并镜像到 TG 后台话题"""

import asyncio
import contextlib
import html
import os
import tempfile
import uuid

import discord
from telegram import (
    InputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    ReactionTypeEmoji,
    ReplyParameters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter

from db.model import DiscordMessageMap, DiscordPost, MessageMap, User

from . import admin_group_id
from . import dc_media
from .dc_common import (
    COMPRESS_SOURCE_LIMIT,
    MTPROTO_UPLOAD_LIMIT,
    NO_MENTIONS,
    TG_PHOTO_LIMIT,
    TG_UPLOAD_LIMIT,
    SentRef,
    TgItem,
    is_transient,
    logger,
    tg_reaction_for,
)
from .dc_format import (
    TG_CAPTION_LIMIT,
    TG_TEXT_LIMIT,
    MentionResolver,
    discord_to_tg_html,
    discord_to_tg_plain,
    human_size,
    render_embed_plain,
    tg_html_visible_len,
)
from .utils import get_topic_status

_VIDEO_EXTS = (".webm", ".mkv", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ts", ".m2ts")
# 长文本按这个长度分段（给后台镜像的标题行留余量，用户和后台的分段保持一致）
TEXT_CHUNK = TG_TEXT_LIMIT - 200
STAGE_USER_SENT, STAGE_DONE = 1, 2


class _Resolver(MentionResolver):
    def __init__(self, message):
        self.message = message
        self.guild = message.guild

    def user(self, user_id):
        for u in getattr(self.message, "mentions", None) or []:
            if u.id == user_id:
                return u.display_name
        m = self.guild.get_member(user_id) if self.guild else None
        return m.display_name if m else None

    def role(self, role_id):
        r = self.guild.get_role(role_id) if self.guild else None
        return r.name if r else None

    def channel(self, channel_id):
        c = self.guild.get_channel_or_thread(channel_id) if self.guild else None
        return c.name if c else None


def _header(name):
    return f"💬 <b>{html.escape(name)}</b> · Discord", f"💬 {name} · Discord"


def _join(*parts):
    return "\n".join(x for x in parts if x)


def _text_chunks(html_text, plain):
    """[(html 或 None, plain)]：放得下就保留格式发一条，否则按行切成纯文本分段"""
    if not plain and not html_text:
        return []
    if tg_html_visible_len(html_text) <= TEXT_CHUNK:
        return [(html_text, plain)]
    chunks, cur = [], ""
    for line in plain.split("\n"):
        while len(line) > TEXT_CHUNK:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:TEXT_CHUNK])
            line = line[TEXT_CHUNK:]
        if cur and len(cur) + 1 + len(line) > TEXT_CHUNK:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return [(None, c) for c in chunks]


class DiscordToTgMixin:
    def _is_admin(self, member):
        if not self.admin_role_ids:
            return True
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        return any(r.id in self.admin_role_ids for r in member.roles)

    async def _on_message(self, message: discord.Message):
        if not self.ready.is_set() or self.failed:
            return
        if message.author.bot or message.webhook_id:
            return
        channel = message.channel
        if not isinstance(channel, discord.Thread) or channel.parent_id != self.forum_channel_id:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        user_id = self._user_by_thread(channel.id)
        if not user_id or not self._is_admin(message.author):
            return
        self._remember(message.id, message.author.display_name, message.content or "[附件]")
        outbox_id = self._outbox_add(user_id, "dc", {"thread_id": channel.id, "message_id": message.id})
        self._submit_outbox(user_id, outbox_id, lambda: self._dc_to_tg(user_id, message), "dc→tg")

    async def _dc_notice(self, message, text):
        try:
            await message.reply(text[:1900], mention_author=False, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass

    # ---------- 收集内容 ----------

    @staticmethod
    def _attachment_kind(att):
        ct = (att.content_type or "").split(";")[0].strip().lower()
        name = att.filename.lower()
        if att.is_voice_message():
            return "voice"
        if ct == "image/gif" or name.endswith(".gif"):
            return "animation"
        if ct in ("image/jpeg", "image/png", "image/webp") or name.endswith((".jpg", ".jpeg", ".png", ".webp")):
            return "photo" if att.size <= TG_PHOTO_LIMIT else "document"
        if ct in ("video/mp4", "video/quicktime") or name.endswith((".mp4", ".mov", ".m4v")):
            return "video"
        if ct.startswith("video/") or name.endswith(_VIDEO_EXTS):
            return "video_other"
        if ct in ("audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a") or name.endswith((".mp3", ".m4a")):
            return "audio"
        return "document"

    async def _download_url(self, url, workdir, filename):
        """流式下载到临时文件，避免大文件占满内存"""
        path = os.path.join(workdir, f"{uuid.uuid4().hex}{os.path.splitext(filename)[1]}")
        async with self.http.get(url, proxy=self.proxy) as resp:
            resp.raise_for_status()
            with open(path, "wb") as fp:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    fp.write(chunk)
        return path

    async def _download_bytes(self, url, limit):
        async with self.http.get(url, proxy=self.proxy) as resp:
            resp.raise_for_status()
            data = b""
            async for chunk in resp.content.iter_chunked(256 * 1024):
                data += chunk
                if len(data) > limit:
                    return None
            return data

    async def _attachment_item(self, att, workdir):
        """返回 (TgItem 或 None, 给用户的说明, 给管理员的说明)"""
        kind = self._attachment_kind(att)
        name = att.filename
        stem = os.path.splitext(name)[0] or "file"
        spoiler = att.is_spoiler()
        try:
            if att.size > TG_UPLOAD_LIMIT:
                if self.mtproto and att.size <= MTPROTO_UPLOAD_LIMIT:
                    path = await self._download_url(att.url, workdir, name)
                    if kind == "video_other":
                        # 先转成 mp4 再上传，保证能在 TG 里直接播放；转不了就按文件发
                        out = await dc_media.to_mp4(path, workdir, MTPROTO_UPLOAD_LIMIT)
                        if out:
                            big = os.path.getsize(out) > TG_UPLOAD_LIMIT
                            return TgItem("video", path=out, filename=f"{stem}.mp4", spoiler=spoiler, big=big), None, None
                        return TgItem("document", path=path, filename=name, big=True), None, None
                    tk = "video" if kind == "video" else "audio" if kind == "audio" else "document"
                    return TgItem(tk, path=path, filename=name, spoiler=spoiler, big=True), None, None
                if kind in ("video", "video_other") and dc_media.has_ffmpeg() and att.size <= COMPRESS_SOURCE_LIMIT:
                    path = await self._download_url(att.url, workdir, name)
                    out = await dc_media.compress_video(path, TG_UPLOAD_LIMIT - 512 * 1024, workdir)
                    if out:
                        return TgItem("video", path=out, filename=f"{stem}.mp4", spoiler=spoiler), None, None
                size = human_size(att.size)
                hint = "" if self.mtproto else "（配置 TG_API_ID / TG_API_HASH 后可发送最大 2GB 的文件）"
                return (
                    None,
                    f"📎 文件 {name}（{size}）过大，未能发送",
                    f"📎 {name}（{size}）超过 Telegram 50MB 上传上限，未发送{hint}",
                )
            data = await att.read()
        except Exception as e:
            if is_transient(e):
                raise
            logger.warning(f"处理 Discord 附件失败 {name}: {e}")
            return None, f"📎 文件 {name} 发送失败", f"📎 {name} 下载/处理失败：{e}"

        if kind == "photo" and dc_media.is_apng(data):
            gif = await dc_media.apng_to_gif(data)
            if gif:
                return TgItem("animation", data=gif, filename=f"{stem}.gif"), None, None
        if kind == "video_other":
            if dc_media.has_ffmpeg():
                src = os.path.join(workdir, f"{uuid.uuid4().hex}{os.path.splitext(name)[1]}")
                with open(src, "wb") as fp:
                    fp.write(data)
                out = await dc_media.to_mp4(src, workdir, TG_UPLOAD_LIMIT)
                if out:
                    return TgItem("video", path=out, filename=f"{stem}.mp4", spoiler=spoiler), None, None
            kind = "document"
        return TgItem(kind, data=data, filename=name, spoiler=spoiler), None, None

    async def _sticker_item(self, sticker):
        """贴纸转换失败只影响这张贴纸本身，不影响同一条消息的其它内容"""
        fmt = sticker.format
        try:
            if fmt == discord.StickerFormatType.png:
                return TgItem("photo", data=await sticker.read(), filename=f"{sticker.name}.png"), None
            if fmt == discord.StickerFormatType.gif:
                return TgItem("animation", data=await sticker.read(), filename=f"{sticker.name}.gif"), None
            gif = None
            if fmt == discord.StickerFormatType.apng:
                gif = await dc_media.apng_to_gif(await sticker.read())
            elif fmt == discord.StickerFormatType.lottie:
                # discord.py 不支持读取 Lottie 贴纸，直接下载 JSON
                data = await self._download_bytes(sticker.url, 5 * 1024 * 1024)
                gif = await dc_media.lottie_to_gif(data) if data else None
            if gif:
                return TgItem("animation", data=gif, filename=f"{sticker.name}.gif"), None
        except Exception as e:
            if is_transient(e):
                raise
            logger.info(f"处理 Discord 贴纸失败 {sticker.name}: {e}")
        return None, f"[贴纸：{sticker.name}]"

    async def _embed_items(self, embed):
        """转发的卡片里的图片下载后作为图片发送，不把 Discord 链接直接发给用户"""
        url = (embed.image and embed.image.url) or (embed.thumbnail and embed.thumbnail.url)
        if not url:
            return []
        try:
            data = await self._download_bytes(url, TG_PHOTO_LIMIT)
        except Exception as e:
            if is_transient(e):
                raise
            logger.info(f"下载卡片图片失败：{e}")
            return []
        return [TgItem("photo", data=data, filename="image.jpg")] if data else []

    @staticmethod
    def _embed_text(embed):
        return render_embed_plain(
            title=embed.title,
            url=embed.url,
            description=embed.description,
            fields=[(f.name, f.value) for f in embed.fields],
            footer=embed.footer.text if embed.footer else None,
            author=embed.author.name if embed.author else None,
        )

    async def _collect(self, message, workdir):
        """返回 (html, plain, items, 用户说明, 管理员说明)"""
        resolver = _Resolver(message)
        blocks_html, blocks_plain = [], []
        items, notes_user, notes_admin = [], [], []

        async def add_attachments(attachments, stickers):
            for att in attachments:
                item, nu, na = await self._attachment_item(att, workdir)
                if item:
                    items.append(item)
                notes_user.extend(x for x in [nu] if x)
                notes_admin.extend(x for x in [na] if x)
            for sticker in stickers:
                item, note = await self._sticker_item(sticker)
                if item:
                    items.append(item)
                if note:
                    blocks_html.append(html.escape(note))
                    blocks_plain.append(note)

        if message.content:
            blocks_html.append(discord_to_tg_html(message.content, resolver))
            blocks_plain.append(discord_to_tg_plain(message.content, resolver))
        await add_attachments(message.attachments, message.stickers)

        # Discord 的「转发」：内容在消息快照里
        for snap in getattr(message, "message_snapshots", None) or []:
            blocks_html.append("<i>🔁 转发的消息</i>")
            blocks_plain.append("🔁 转发的消息")
            if snap.content:
                blocks_html.append(discord_to_tg_html(snap.content, resolver))
                blocks_plain.append(discord_to_tg_plain(snap.content, resolver))
            for embed in snap.embeds:
                if embed.type == "rich":
                    text = self._embed_text(embed)
                    if text:
                        blocks_html.append(f"<blockquote>{html.escape(text)}</blockquote>")
                        blocks_plain.append(text)
                    items.extend(await self._embed_items(embed))
            await add_attachments(snap.attachments, getattr(snap, "stickers", []))

        poll = getattr(message, "poll", None)
        if poll:
            text = "\n".join([f"📊 投票：{poll.question}"] + [f"▫️ {a.text}" for a in poll.answers])
            blocks_html.append(html.escape(text))
            blocks_plain.append(text)
        return _join(*blocks_html), _join(*blocks_plain), items, notes_user, notes_admin

    # ---------- 发送 ----------

    def _dc_post(self, message_id):
        return self.db.query(DiscordPost).filter(DiscordPost.source == f"dc:{message_id}").first()

    async def _discard_partial_dc(self, post, user_id):
        """撤回上次只发了一部分的 TG 消息，以便整条重发"""
        rows = self.db.query(DiscordMessageMap).filter(DiscordMessageMap.post_id == post.id).all()
        for row in rows:
            for chat_id, mid in ((user_id, row.user_chat_message_id), (admin_group_id, row.group_chat_message_id)):
                if mid:
                    with contextlib.suppress(Exception):
                        await self.bot.delete_message(chat_id, mid)
            self.db.query(MessageMap).filter(
                MessageMap.user_id == user_id, MessageMap.user_chat_message_id == row.user_chat_message_id
            ).delete(synchronize_session="fetch")
            self.db.delete(row)
        self.db.delete(post)
        self._commit()

    async def _dc_to_tg(self, user_id, message):
        post = self._dc_post(message.id)
        if post and post.stage >= STAGE_DONE:
            return  # 已经同步过（补发时去重）
        u = self.db.query(User).filter(User.user_id == user_id).first()
        tg_thread = u.message_thread_id if u else 0
        header = _header(message.author.display_name)
        if post and post.stage == STAGE_USER_SENT:
            # 上次已发给用户，只差后台镜像
            await self._mirror_group_by_copy(user_id, post, tg_thread)
            return
        if post:
            await self._discard_partial_dc(post, user_id)
        if tg_thread and get_topic_status(self.db, tg_thread) == "closed":
            await self._dc_notice(message, "⚠️ 对话已关闭，消息未发送。请先解锁帖子（或在 Telegram 重新打开话题）再回复。")
            return

        has_media = bool(message.attachments or message.stickers or getattr(message, "message_snapshots", None))
        async with (self.media_sem if has_media else contextlib.nullcontext()):
            with tempfile.TemporaryDirectory(prefix="dcbridge-") as workdir:
                await self._dc_send(user_id, message, tg_thread, header, workdir)

    async def _dc_send(self, user_id, message, tg_thread, header, workdir):
        html_text, plain, items, notes_user, notes_admin = await self._collect(message, workdir)
        user_html = _join(html_text, html.escape("\n".join(notes_user)))
        user_plain = _join(plain, "\n".join(notes_user))
        group_html = _join(html_text, html.escape("\n".join(notes_admin)))
        group_plain = _join(plain, "\n".join(notes_admin))
        if notes_admin:
            await self._dc_notice(message, "⚠️ 部分附件未能发送给用户：\n" + "\n".join(notes_admin))
        if not user_html and not items:
            await self._dc_notice(message, "⚠️ 这条消息没有可以发送到 Telegram 的内容（例如只有 embed 或不支持的贴纸），未发送。")
            return

        user_reply = group_reply = None
        ref = message.reference
        if ref and ref.message_id and getattr(ref, "type", None) != discord.MessageReferenceType.forward:
            row = self._dmm_first(DiscordMessageMap.discord_message_id == ref.message_id)
            if row:
                user_reply, group_reply = row.user_chat_message_id, row.group_chat_message_id

        text_separate = any(i.big for i in items) or (
            bool(items) and tg_html_visible_len(_join(header[1], group_html)) > TG_CAPTION_LIMIT
        )
        caption_holder = None if (user_html and (text_separate or not items)) else 0
        post = DiscordPost(
            user_id=user_id,
            thread_id=message.channel.id,
            username=message.author.display_name,
            source=f"dc:{message.id}",
            stage=0,
        )
        self.db.add(post)
        self._commit()
        rows = {}

        def on_user(role, msg):
            if isinstance(role, str):  # 文字分段 "text" / "text:1" …
                text_role, part = "text", int(role.split(":")[1]) if ":" in role else 0
            else:
                text_role, part = ("caption" if role == caption_holder else None), 0
            mm = MessageMap(user_chat_message_id=msg.message_id, group_chat_message_id=None, user_id=user_id)
            dmm = DiscordMessageMap(
                user_id=user_id,
                user_chat_message_id=msg.message_id,
                discord_message_id=message.id,
                discord_thread_id=message.channel.id,
                part=part,
                origin="dc_admin",
                text_role=text_role,
                post_id=post.id,
            )
            self.db.add_all([mm, dmm])
            self._commit()
            rows[role] = (mm, dmm)

        def on_group(role, msg):
            if role in rows:
                mm, dmm = rows[role]
                mm.group_chat_message_id = dmm.group_chat_message_id = msg.message_id
                self._commit()

        try:
            user_sent = await self._send_tg(
                user_id,
                user_html,
                user_plain,
                list(enumerate(items)),
                reply_to=user_reply,
                text_separate=text_separate,
                on_sent=on_user,
            )
        except Forbidden:
            post.stage = STAGE_DONE
            self._commit()
            await self._dc_notice(message, "❌ 发送失败：用户已屏蔽机器人。")
            return
        except BadRequest as e:
            post.stage = STAGE_DONE
            self._commit()
            logger.warning(f"Discord → Telegram 发送失败：{e}")
            await self._dc_notice(message, f"❌ 发送失败：{e}")
            return
        post.stage = STAGE_USER_SENT
        self._commit()

        if tg_thread:
            group_items = []
            for role, msg in user_sent:
                if not isinstance(role, int):
                    continue
                if items[role].big:
                    group_items.append((role, TgItem("copy", copy_from=(user_id, msg.message_id))))
                elif item := self._item_from_sent(msg):
                    group_items.append((role, item))
            try:
                await self._send_tg(
                    admin_group_id,
                    group_html,
                    group_plain,
                    group_items,
                    reply_to=group_reply,
                    thread_id=tg_thread,
                    header=header,
                    text_separate=text_separate,
                    on_sent=on_group,
                )
            except Exception as e:
                if is_transient(e):
                    raise  # 重试时只补后台镜像
                logger.warning(f"Discord 消息镜像到 TG 后台失败：{e}")
        post.stage = STAGE_DONE
        self._commit()

    async def _mirror_group_by_copy(self, user_id, post, tg_thread):
        """补后台镜像：发一行标题，再逐条复制已发给用户的消息"""
        if tg_thread:
            rows = (
                self.db.query(DiscordMessageMap)
                .filter(DiscordMessageMap.post_id == post.id)
                .order_by(DiscordMessageMap.id)
                .all()
            )
            pending = [r for r in rows if not r.group_chat_message_id]
            if pending:
                header = _header(post.username or "Discord")
                await self._tg_retry(
                    lambda: self.bot.send_message(admin_group_id, header[0], parse_mode=ParseMode.HTML, message_thread_id=tg_thread)
                )
            for row in pending:
                copied = await self._tg_retry(
                    lambda: self.bot.copy_message(admin_group_id, user_id, row.user_chat_message_id, message_thread_id=tg_thread)
                )
                row.group_chat_message_id = copied.message_id
                self.db.query(MessageMap).filter(
                    MessageMap.user_id == user_id, MessageMap.user_chat_message_id == row.user_chat_message_id
                ).update({MessageMap.group_chat_message_id: copied.message_id}, synchronize_session="fetch")
                self._commit()
        post.stage = STAGE_DONE
        self._commit()

    @staticmethod
    def _item_from_sent(msg):
        spoiler = bool(getattr(msg, "has_media_spoiler", False))
        if getattr(msg, "photo", None):
            return TgItem("photo", file_id=msg.photo[-1].file_id, spoiler=spoiler)
        if getattr(msg, "animation", None):
            return TgItem("animation", file_id=msg.animation.file_id, spoiler=spoiler)
        if getattr(msg, "video", None):
            return TgItem("video", file_id=msg.video.file_id, spoiler=spoiler)
        if getattr(msg, "voice", None):
            return TgItem("voice", file_id=msg.voice.file_id)
        if getattr(msg, "audio", None):
            return TgItem("audio", file_id=msg.audio.file_id)
        if getattr(msg, "document", None):
            return TgItem("document", file_id=msg.document.file_id)
        return None

    @staticmethod
    def _media_source(item):
        if item.file_id:
            return item.file_id
        if item.path:
            with open(item.path, "rb") as fp:
                return InputFile(fp.read(), filename=item.filename)
        return InputFile(item.data, filename=item.filename)

    @staticmethod
    def _is_parse_error(e):
        return "parse" in str(e).lower() and "entit" in str(e).lower()

    @staticmethod
    async def _tg_retry(factory):
        """遇到 Telegram 限流（RetryAfter）时等待后重试"""
        for _ in range(3):
            try:
                return await factory()
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
        return await factory()

    async def _with_parse_fallback(self, send, html_value, plain_value):
        """先按 HTML 发送；HTML 解析失败（格式交叉等）时退回纯文本。html_value 为 None 时直接发纯文本"""
        if html_value is None:
            return await self._tg_retry(lambda: send(plain_value, None))
        try:
            return await self._tg_retry(lambda: send(html_value, ParseMode.HTML))
        except BadRequest as e:
            if not self._is_parse_error(e):
                raise
            return await self._tg_retry(lambda: send(plain_value, None))

    def _input_media(self, item, caption, parse_mode):
        src = self._media_source(item)
        cap = {"caption": caption, "parse_mode": parse_mode} if caption else {}
        if item.kind == "photo":
            return InputMediaPhoto(src, has_spoiler=item.spoiler or None, **cap)
        if item.kind == "video":
            return InputMediaVideo(src, has_spoiler=item.spoiler or None, supports_streaming=True, **cap)
        if item.kind == "audio":
            return InputMediaAudio(src, **cap)
        return InputMediaDocument(src, **cap)

    async def _send_single(self, chat_id, item, caption, kw):
        """caption: (html, plain) 或 None"""
        bot = self.bot
        if item.big:
            reply = kw.get("reply_parameters")
            mid = await self.mtproto.send_file(
                chat_id, item.path, kind=item.kind, filename=item.filename, reply_to=reply.message_id if reply else None
            )
            return SentRef(mid)
        if item.kind == "copy":
            from_chat, mid = item.copy_from

            async def send_copy(value, parse_mode):
                extra = {"caption": value, "parse_mode": parse_mode} if value else {}
                return await bot.copy_message(chat_id, from_chat, mid, **kw, **extra)

            if caption:
                result = await self._with_parse_fallback(send_copy, *caption)
            else:
                result = await self._tg_retry(lambda: send_copy(None, None))
            return SentRef(result.message_id)

        async def send(kind, value, parse_mode):
            src = self._media_source(item)
            extra = dict(kw)
            if value:
                extra.update(caption=value, parse_mode=parse_mode)
            if kind == "photo":
                return await bot.send_photo(chat_id, src, has_spoiler=item.spoiler or None, **extra)
            if kind == "video":
                return await bot.send_video(chat_id, src, has_spoiler=item.spoiler or None, supports_streaming=True, **extra)
            if kind == "animation":
                return await bot.send_animation(chat_id, src, has_spoiler=item.spoiler or None, **extra)
            if kind == "audio":
                return await bot.send_audio(chat_id, src, **extra)
            if kind == "voice":
                return await bot.send_voice(chat_id, src, **extra)
            return await bot.send_document(chat_id, src, **extra)

        kind = item.kind
        for _ in range(3):
            try:
                if caption:
                    return await self._with_parse_fallback(lambda v, pm: send(kind, v, pm), *caption)
                return await self._tg_retry(lambda: send(kind, None, None))
            except BadRequest as e:
                # 图片尺寸/格式不被接受、语音格式不对等，改用文件发送
                if kind != "document" and not item.file_id:
                    logger.info(f"以 {kind} 发送失败，改为文件：{e}")
                    kind = "document"
                    continue
                raise

    async def _send_tg(
        self, chat_id, html_text, plain, items, *, reply_to=None, thread_id=None, header=None, text_separate=False, on_sent=None
    ):
        """发送到 Telegram。items: [(序号, TgItem)]；返回 [(角色, Message)]，
        角色为媒体序号，或文字分段 "text" / "text:1" / "text:2"…。每发出一条就回调 on_sent(角色, Message)"""
        bot = self.bot
        base = {"message_thread_id": thread_id} if thread_id else {}
        pending_reply = [reply_to]
        sent = []

        def with_reply():
            kw = dict(base)
            if pending_reply[0]:
                kw["reply_parameters"] = ReplyParameters(message_id=pending_reply[0], allow_sending_without_reply=True)
                pending_reply[0] = None
            return kw

        def emit(role, msg):
            sent.append((role, msg))
            if on_sent:
                on_sent(role, msg)

        caption = None
        if html_text and (text_separate or not items):
            for k, (h, p) in enumerate(_text_chunks(html_text, plain)):
                self._check_alive()
                head = header if (header and k == 0) else None
                kw = with_reply()
                value_h = _join(head[0], h) if (head and h) else h
                value_p = _join(head[1] if head else None, p)
                msg = await self._with_parse_fallback(
                    lambda v, pm: bot.send_message(chat_id, v, parse_mode=pm, **kw), value_h, value_p
                )
                emit("text" if k == 0 else f"text:{k}", msg)
        elif html_text or header:
            caption = (_join(header[0] if header else None, html_text), _join(header[1] if header else None, plain))

        groupable = {"photo": "visual", "video": "visual", "audio": "audio", "document": "document"}
        runs = []
        for idx, item in items:
            cat = None if (item.big or item.kind == "copy") else groupable.get(item.kind)
            if runs and cat and runs[-1][0] == cat:
                runs[-1][1].append((idx, item))
            else:
                runs.append((cat, [(idx, item)]))

        for cat, run in runs:
            self._check_alive()
            if cat and len(run) >= 2:
                for start in range(0, len(run), 10):
                    chunk = run[start : start + 10]
                    kw = with_reply()
                    first_caption = caption

                    def build(use_plain):
                        return [
                            self._input_media(
                                item,
                                (first_caption[1 if use_plain else 0] if (k == 0 and first_caption) else None),
                                None if use_plain else ParseMode.HTML,
                            )
                            for k, (_, item) in enumerate(chunk)
                        ]

                    try:
                        try:
                            msgs = await self._tg_retry(lambda: bot.send_media_group(chat_id, build(False), **kw))
                        except BadRequest as e:
                            if not (first_caption and self._is_parse_error(e)):
                                raise
                            msgs = await self._tg_retry(lambda: bot.send_media_group(chat_id, build(True), **kw))
                        for (idx, _), m in zip(chunk, msgs):
                            emit(idx, m)
                    except BadRequest as e:
                        logger.info(f"媒体组发送失败，改为逐个发送：{e}")
                        for k, (idx, item) in enumerate(chunk):
                            cap = first_caption if k == 0 else None
                            emit(idx, await self._send_single(chat_id, item, cap, kw if k == 0 else dict(base)))
                    caption = None
            else:
                for idx, item in run:
                    emit(idx, await self._send_single(chat_id, item, caption, with_reply()))
                    caption = None
        return sent

    # ---------- 编辑 / 删除 / 表情 ----------

    async def _on_raw_message_edit(self, payload):
        if not self.ready.is_set() or self.failed:
            return
        data = payload.data
        if data.get("webhook_id") or data.get("author", {}).get("bot"):
            return
        edited_at = data.get("edited_timestamp")
        # 链接预览展开等也会触发更新事件，只处理真正的编辑
        if not edited_at or self._edit_seen.get(payload.message_id) == edited_at:
            return
        user_id = self._user_by_thread(payload.channel_id)
        if not user_id:
            return
        message = payload.message
        # 角色被撤销后不能再通过编辑旧消息给用户发内容
        if not self._is_admin(message.author):
            return
        outbox_id = self._outbox_add(user_id, "dc_edit", {"thread_id": payload.channel_id, "message_id": payload.message_id})
        self._submit_outbox(user_id, outbox_id, lambda: self._dc_edit_job(user_id, message, edited_at), "dc→tg edit")

    def _mark_edit_seen(self, message_id, edited_at):
        self._edit_seen[message_id] = edited_at
        while len(self._edit_seen) > 2000:
            self._edit_seen.popitem(last=False)

    async def _dc_edit_job(self, user_id, message, edited_at):
        if self._edit_seen.get(message.id) == edited_at:
            return
        rows = (
            self.db.query(DiscordMessageMap)
            .filter(
                DiscordMessageMap.discord_message_id == message.id,
                DiscordMessageMap.origin == "dc_admin",
                DiscordMessageMap.text_role.in_(("text", "caption")),
            )
            .order_by(DiscordMessageMap.part, DiscordMessageMap.id)
            .all()
        )
        if not rows:
            return
        resolver = _Resolver(message)
        html_text = discord_to_tg_html(message.content, resolver)
        plain = discord_to_tg_plain(message.content, resolver)
        header = _header(message.author.display_name)
        self._remember(message.id, message.author.display_name, message.content or "[附件]")

        texts = [r for r in rows if r.text_role == "text"]
        for row in rows:
            if row.text_role != "caption":
                continue
            if tg_html_visible_len(_join(header[1], html_text)) > TG_CAPTION_LIMIT:
                await self._caption_to_text(user_id, row, html_text, plain, header)
            else:
                await self._edit_tg_text(user_id, row.user_chat_message_id, "caption", html_text, plain, None)
                await self._edit_tg_text(admin_group_id, row.group_chat_message_id, "caption", html_text, plain, header)
        if texts:
            await self._edit_text_segments(user_id, texts, html_text, plain, header)
        self._mark_edit_seen(message.id, edited_at)

    async def _edit_text_segments(self, user_id, texts, html_text, plain, header):
        """文字分段整体对齐：编辑已有分段、追加新分段、删除多余分段（用户与后台分段一致）"""
        chunks = _text_chunks(html_text, plain) or [(None, "（已清空）")]
        u = self.db.query(User).filter(User.user_id == user_id).first()
        tg_thread = u.message_thread_id if u else 0
        first = texts[0]
        for k, (h, p) in enumerate(chunks):
            if k < len(texts):
                row = texts[k]
                await self._edit_tg_text(user_id, row.user_chat_message_id, "text", h, p, None)
                await self._edit_tg_text(admin_group_id, row.group_chat_message_id, "text", h, p, header if k == 0 else None)
                continue
            user_msg = await self._with_parse_fallback(
                lambda v, pm: self.bot.send_message(
                    user_id, v, parse_mode=pm,
                    reply_parameters=ReplyParameters(message_id=first.user_chat_message_id, allow_sending_without_reply=True),
                ),
                h, p,
            )
            group_mid = None
            if tg_thread and first.group_chat_message_id:
                group_msg = await self._with_parse_fallback(
                    lambda v, pm: self.bot.send_message(
                        admin_group_id, v, parse_mode=pm, message_thread_id=tg_thread,
                        reply_parameters=ReplyParameters(message_id=first.group_chat_message_id, allow_sending_without_reply=True),
                    ),
                    h, p,
                )
                group_mid = group_msg.message_id
            self.db.add(MessageMap(user_chat_message_id=user_msg.message_id, group_chat_message_id=group_mid, user_id=user_id))
            self.db.add(
                DiscordMessageMap(
                    user_id=user_id,
                    user_chat_message_id=user_msg.message_id,
                    group_chat_message_id=group_mid,
                    discord_message_id=first.discord_message_id,
                    discord_thread_id=first.discord_thread_id,
                    part=k,
                    origin="dc_admin",
                    text_role="text",
                    post_id=first.post_id,
                )
            )
            self._commit()
        for row in texts[len(chunks):]:
            for chat_id, mid in ((user_id, row.user_chat_message_id), (admin_group_id, row.group_chat_message_id)):
                if mid:
                    with contextlib.suppress(Exception):
                        await self.bot.delete_message(chat_id, mid)
            self.db.delete(row)
            self._commit()

    async def _edit_tg_text(self, chat_id, message_id, role, html_text, plain, header):
        """html_text 为 None 时按纯文本编辑"""
        if not message_id:
            return
        h = _join(header[0], html_text) if (header and html_text is not None) else html_text
        p = _join(header[1] if header else None, plain)
        if role == "text" and not p:
            return
        if role == "text":

            async def send(value, parse_mode):
                return await self.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=value, parse_mode=parse_mode
                )

        else:
            if html_text is not None and not h:
                h = None

            async def send(value, parse_mode):
                return await self.bot.edit_message_caption(
                    chat_id=chat_id, message_id=message_id, caption=value or None, parse_mode=parse_mode
                )

        try:
            await self._with_parse_fallback(send, h, p)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"同步 Discord 编辑到 Telegram 失败：{e}")

    async def _caption_to_text(self, user_id, row, html_text, plain, header):
        """编辑后超过 1024 字放不进图片说明：清空说明，改为回复该媒体的独立文字消息"""
        await self._edit_tg_text(user_id, row.user_chat_message_id, "caption", "", "", None)
        await self._edit_tg_text(admin_group_id, row.group_chat_message_id, "caption", "", "", header)
        u = self.db.query(User).filter(User.user_id == user_id).first()
        tg_thread = u.message_thread_id if u else 0
        user_msg = await self._send_tg(user_id, html_text, plain, [], reply_to=row.user_chat_message_id)
        group_msg = []
        if tg_thread and row.group_chat_message_id:
            group_msg = await self._send_tg(
                admin_group_id, html_text, plain, [], reply_to=row.group_chat_message_id, thread_id=tg_thread, header=header
            )
        row.text_role = None
        for k, (_, umsg) in enumerate(user_msg):
            gmid = group_msg[k][1].message_id if k < len(group_msg) else None
            self.db.add(MessageMap(user_chat_message_id=umsg.message_id, group_chat_message_id=gmid, user_id=user_id))
            self.db.add(
                DiscordMessageMap(
                    user_id=user_id,
                    user_chat_message_id=umsg.message_id,
                    group_chat_message_id=gmid,
                    discord_message_id=row.discord_message_id,
                    discord_thread_id=row.discord_thread_id,
                    part=k,
                    origin="dc_admin",
                    text_role="text",
                    post_id=row.post_id,
                )
            )
        self._commit()

    async def _on_raw_message_delete(self, payload):
        user_id = self._user_by_thread(payload.channel_id)
        if user_id:
            self._submit_delete(user_id, [payload.message_id])

    async def _on_raw_bulk_message_delete(self, payload):
        user_id = self._user_by_thread(payload.channel_id)
        if user_id:
            self._submit_delete(user_id, list(payload.message_ids))

    def _submit_delete(self, user_id, ids):
        outbox_id = self._outbox_add(user_id, "dc_delete", {"message_ids": ids})
        self._submit_outbox(user_id, outbox_id, lambda: self._dc_delete_job(user_id, ids), "dc→tg delete")

    async def _dc_delete_job(self, user_id, message_ids):
        """只删除由 Discord 管理员发出的消息在 TG 两侧的副本"""
        rows = (
            self.db.query(DiscordMessageMap)
            .filter(DiscordMessageMap.discord_message_id.in_(message_ids), DiscordMessageMap.origin == "dc_admin")
            .all()
        )
        for row in rows:
            for chat_id, mid in ((user_id, row.user_chat_message_id), (admin_group_id, row.group_chat_message_id)):
                if not mid:
                    continue
                try:
                    await self.bot.delete_message(chat_id, mid)
                except Exception as e:
                    if is_transient(e):
                        raise
                    logger.info(f"同步删除到 Telegram 失败（可能超过 48 小时）：{e}")
            self.db.delete(row)
            self._commit()

    async def _on_raw_reaction(self, payload, *, add):
        if not self.ready.is_set() or self.failed:
            return
        if payload.user_id == self.client.user.id or payload.emoji.is_custom_emoji():
            return
        user_id = self._user_by_thread(payload.channel_id)
        if not user_id:
            return
        key = (payload.message_id, payload.emoji.name)
        if add:
            if payload.member is None or payload.member.bot or not self._is_admin(payload.member):
                return
            self._reactors[key] = payload.user_id
            while len(self._reactors) > 5000:
                self._reactors.popitem(last=False)
        elif self._reactors.get(key) != payload.user_id:
            # 只有添加该表情的那位管理员移除时才同步，普通成员的操作不影响 TG
            return
        else:
            self._reactors.pop(key, None)
        emoji = tg_reaction_for(payload.emoji.name)
        if not emoji:
            return

        async def job():
            row = self._dmm_first(DiscordMessageMap.discord_message_id == payload.message_id)
            if not row:
                return
            reaction = [ReactionTypeEmoji(emoji)] if add else []
            for chat_id, mid in ((user_id, row.user_chat_message_id), (admin_group_id, row.group_chat_message_id)):
                if not mid:
                    continue
                try:
                    await self.bot.set_message_reaction(chat_id, mid, reaction)
                except Exception as e:
                    logger.info(f"同步表情到 Telegram 失败：{e}")

        self._submit(user_id, job, "dc→tg reaction")
