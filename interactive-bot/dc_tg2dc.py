"""Telegram → Discord：把用户消息、TG 管理员回复同步到 Discord 帖子"""

import os
import tempfile
import uuid

import discord
from telegram import ReactionTypeEmoji
from telegram.error import BadRequest

from db.model import DiscordMessageMap, DiscordPost, User

from . import dc_media
from .dc_common import (
    DC_THREAD_ARCHIVED,
    DC_THREAD_LOCKED,
    DC_TOO_LARGE,
    DC_UNKNOWN_CHANNEL,
    DC_UNKNOWN_EMOJI,
    DC_UNKNOWN_MESSAGE,
    DC_UNKNOWN_WEBHOOK,
    NO_MENTIONS,
    TG_DOWNLOAD_LIMIT,
    OutFile,
    OutMessage,
    ThreadGone,
    Upload,
    is_transient,
    logger,
    tg_message_link,
)
from .dc_format import (
    ents_from_ptb,
    escape_md,
    human_size,
    plain_snippet,
    render_contact,
    render_dice,
    render_game,
    render_giveaway,
    render_invoice,
    render_location,
    render_paid_media,
    render_poll,
    render_story,
    render_venue,
    sanitize_webhook_name,
    split_discord,
    tg_to_discord,
)
from .utils import to_simplified

_COMPRESSIBLE = ("video", "animation", "video_note")
STAGE_DONE = 2


def _compose(header, bodies, footer):
    return "\n".join(x for x in [header, *bodies, footer] if x)


def _stem(filename):
    return os.path.splitext(filename)[0] or "file"


class TgToDiscordMixin:
    # ---------- 发送到帖子 ----------

    def _upload_limit(self):
        guild = self.forum.guild if self.forum else None
        return guild.filesize_limit if guild else 10 * 1024 * 1024

    async def _unarchive(self, thread_id):
        thread = await self._fetch_thread(thread_id)
        if thread.archived:
            await thread.edit(archived=False)

    async def _bot_send(self, thread_id, content, uploads, username):
        """帖子被锁定时 webhook 无法发言，改由机器人（有管理帖子权限）代发，保留锁定状态"""
        thread = await self._fetch_thread(thread_id)
        kwargs = dict(allowed_mentions=NO_MENTIONS)
        kwargs["content"] = "\n".join(x for x in (f"-# 💬 {escape_md(username)}", content) if x)
        if uploads:
            kwargs["files"] = [u.to_file() for u in uploads]
        return await thread.send(**kwargs)

    async def _webhook_send(self, thread_id, *, content, uploads, username):
        self._check_alive()
        uploads = list(uploads)
        for _ in range(4):
            await self.webhook_pacer.wait()
            kwargs = dict(
                username=username,
                thread=discord.Object(id=thread_id),
                wait=True,
                allowed_mentions=NO_MENTIONS,
            )
            if content:
                kwargs["content"] = content
            if uploads:
                kwargs["files"] = [u.to_file() for u in uploads]
            try:
                return await self.webhook.send(**kwargs)
            except discord.HTTPException as e:
                if e.code == DC_UNKNOWN_WEBHOOK:
                    await self._ensure_webhook(force=True)
                elif e.code == DC_UNKNOWN_CHANNEL:
                    raise ThreadGone(thread_id)
                elif e.code == DC_THREAD_ARCHIVED:
                    await self._unarchive(thread_id)
                elif e.code == DC_THREAD_LOCKED:
                    return await self._bot_send(thread_id, content, uploads, username)
                elif uploads and (e.status == 413 or e.code == DC_TOO_LARGE):
                    notes = "\n".join(
                        f"-# 📎 {u.label} {escape_md(u.filename)}（{human_size(u.size)}）未同步：超出 Discord 上传限制"
                        for u in uploads
                    )
                    content = f"{content}\n{notes}" if content else notes
                    uploads = []
                else:
                    raise
        raise RuntimeError("Discord 发送重试次数过多")

    async def _edit_dc_message(self, thread_id, message_id, content, username):
        """编辑 webhook 发出的消息；锁帖时由机器人代发的消息走机器人编辑。content 为 None 表示清空文字"""
        try:
            await self.webhook.edit_message(
                message_id, content=content, thread=discord.Object(id=thread_id), allowed_mentions=NO_MENTIONS
            )
            return
        except discord.HTTPException as e:
            if e.code == DC_THREAD_ARCHIVED:
                await self._unarchive(thread_id)
                return await self._edit_dc_message(thread_id, message_id, content, username)
            if e.code not in (DC_UNKNOWN_MESSAGE, DC_UNKNOWN_WEBHOOK) and e.status not in (401, 403):
                raise
        message = self.client.get_partial_messageable(thread_id).get_partial_message(message_id)
        text = "\n".join(x for x in (f"-# 💬 {escape_md(username)}", content) if x)
        await message.edit(content=text, allowed_mentions=NO_MENTIONS)

    async def _delete_dc_message(self, thread_id, message_id):
        try:
            await self.webhook.delete_message(message_id, thread=discord.Object(id=thread_id))
        except discord.HTTPException:
            try:
                await self.client.get_partial_messageable(thread_id).get_partial_message(message_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                logger.info(f"删除 Discord 消息失败 {message_id}: {e}")

    # ---------- 文件准备 ----------

    @staticmethod
    def _file_note(f, size, reason):
        link = f" · [在 Telegram 中查看](<{f.link}>)" if f.link else ""
        return f"-# 📎 {f.label} {escape_md(f.filename)}（{human_size(size)}）未同步：{reason}{link}"

    async def _prepare_file(self, f, workdir):
        """下载 → 转换（动态贴纸转 GIF）→ 超限压缩。返回 (Upload 或 None, 说明文字或 None)。
        大文件留在 workdir 的临时文件里，由调用方在上传完成后统一清理"""
        limit = self._upload_limit()
        compressible = f.kind in _COMPRESSIBLE and dc_media.has_ffmpeg()
        if f.size and f.size > limit and f.kind not in ("photo", "sticker", "sticker_tgs", "sticker_video"):
            if not compressible:
                return None, self._file_note(f, f.size, f"超出 Discord 上传限制（{human_size(limit)}）")
            if not dc_media.can_compress(limit, f.duration):
                return None, self._file_note(f, f.size, "视频太长，压缩后无法放进 Discord 上传限制")

        data, path = None, None
        try:
            if f.fetch is not None and not (f.size and f.size > TG_DOWNLOAD_LIMIT):
                try:
                    data = await f.fetch()
                except BadRequest as e:
                    if "too big" not in str(e).lower():
                        raise
            if data is None:
                if f.fetch_to is None:
                    return None, self._file_note(
                        f, f.size, "超出 Telegram 机器人 20MB 下载限制（配置 TG_API_ID / TG_API_HASH 后可同步）"
                    )
                path = os.path.join(workdir, f"src-{uuid.uuid4().hex}{os.path.splitext(f.filename)[1]}")
                await f.fetch_to(path)
        except Exception as e:
            if is_transient(e):
                raise
            logger.warning(f"下载 Telegram 文件失败 {f.filename}: {e}")
            return None, self._file_note(f, f.size, "下载失败")

        def as_path():
            nonlocal path
            if path is None:
                path = os.path.join(workdir, f"src-{uuid.uuid4().hex}{os.path.splitext(f.filename)[1]}")
                with open(path, "wb") as fp:
                    fp.write(data)
            return path

        def as_bytes():
            if data is not None:
                return data
            with open(path, "rb") as fp:
                return fp.read()

        filename = f.filename
        if f.kind == "sticker_tgs":
            gif = await dc_media.tgs_to_gif(as_bytes())
            if gif is None:
                return await self._prepare_file(f.fallback, workdir) if f.fallback else (None, None)
            data, path, filename = gif, None, "sticker.gif"
        elif f.kind == "sticker_video":
            gif = await dc_media.video_to_gif(as_path(), workdir)
            if gif is not None:
                data, path, filename = gif, None, "sticker.gif"

        size = len(data) if data is not None else os.path.getsize(path)
        if size > limit:
            if compressible:
                out = await dc_media.compress_video(as_path(), limit, workdir)
                if out:
                    data, path, filename = None, out, f"{_stem(filename)}.mp4"
            elif f.kind in ("photo", "sticker"):
                shrunk = await dc_media.shrink_image(as_bytes(), limit)
                if shrunk:
                    data, path, filename = shrunk, None, f"{_stem(filename)}.jpg"
            size = len(data) if data is not None else os.path.getsize(path)
            if size > limit:
                return None, self._file_note(f, size, f"超出 Discord 上传限制（{human_size(limit)}）")
        return Upload(filename, size, f.spoiler, f.label, f.source, data=data, path=path), None

    async def _prepare_files(self, files, workdir):
        uploads, notes = [], []
        for f in files:
            upload, note = await self._prepare_file(f, workdir)
            if upload:
                uploads.append(upload)
            if note:
                notes.append(note)
        return uploads, notes

    def _group_uploads(self, uploads):
        limit = self._upload_limit()
        groups, cur, cur_size = [], [], 0
        for u in uploads:
            if cur and (len(cur) >= 10 or cur_size + u.size > limit):
                groups.append(cur)
                cur, cur_size = [], 0
            cur.append(u)
            cur_size += u.size
        if cur:
            groups.append(cur)
        return groups

    # ---------- 发帖与映射 ----------

    def _remember(self, message_id, author, text):
        self._snippets[message_id] = (author, plain_snippet(text))
        while len(self._snippets) > 5000:
            self._snippets.popitem(last=False)

    async def _post(self, user_id, thread_id, out, *, username, prefix=(), origin, pairs):
        """发送 OutMessage。带文件时受全局并发限制，临时文件保留到上传完成"""
        if not out.files:
            return await self._post_prepared(user_id, thread_id, out, [], [], username, prefix, origin, pairs)
        async with self.media_sem:
            with tempfile.TemporaryDirectory(prefix="dcbridge-") as workdir:
                uploads, notes = await self._prepare_files(out.files, workdir)
                return await self._post_prepared(user_id, thread_id, out, uploads, notes, username, prefix, origin, pairs)

    async def _post_prepared(self, user_id, thread_id, out, uploads, notes, username, prefix, origin, pairs):
        """每发出一条 Discord 消息立刻写映射；全部发完把 DiscordPost.stage 标记为完成"""
        header_lines = list(prefix)
        if out.forward_from:
            header_lines.append(f"-# 🔁 转发自 {escape_md(out.forward_from)}")
        header = "\n".join(x for x in header_lines if x)
        footer = "\n".join(x for x in out.extras + notes if x)
        content = _compose(header, [out.bodies[k] for k in sorted(out.bodies)], footer)
        parts = split_discord(content)
        groups = self._group_uploads(uploads)
        if not parts and not groups:
            parts = ["-# （空消息）"]

        post = None
        if pairs:
            post = DiscordPost(user_id=user_id, thread_id=thread_id, username=username, header=header, footer=footer, stage=0)
            self.db.add(post)
            self._commit()
        item_rows = {}
        sent_ids = []

        def record(message, files, text_part):
            if not sent_ids:
                for i, (user_msg_id, group_msg_id) in enumerate(pairs):
                    row = DiscordMessageMap(
                        user_id=user_id,
                        user_chat_message_id=user_msg_id,
                        group_chat_message_id=group_msg_id,
                        discord_message_id=message.id,
                        discord_thread_id=thread_id,
                        part=0,
                        origin=origin,
                        post_id=post.id,
                        body=out.bodies.get(i, ""),
                    )
                    self.db.add(row)
                    item_rows[i] = row
            sent_ids.append(message.id)
            if text_part is not None and pairs:
                user_msg_id, group_msg_id = pairs[0]
                self.db.add(
                    DiscordMessageMap(
                        user_id=user_id,
                        user_chat_message_id=user_msg_id,
                        group_chat_message_id=group_msg_id,
                        discord_message_id=message.id,
                        discord_thread_id=thread_id,
                        part=text_part,
                        origin=origin,
                        text_role="text",
                        post_id=post.id,
                        has_files=bool(files),
                    )
                )
            # 附件可能被拆到后面的消息里：让每条 TG 消息指向真正承载它附件的 Discord 消息
            for u in files:
                if u.source in item_rows:
                    item_rows[u.source].discord_message_id = message.id
            self._commit()

        for idx, part in enumerate(parts):
            files = groups.pop(0) if idx == len(parts) - 1 and groups else []
            message = await self._webhook_send(thread_id, content=part, uploads=files, username=username)
            record(message, files, idx)
        for files in groups:
            message = await self._webhook_send(thread_id, content=None, uploads=files, username=username)
            record(message, files, None)
        if post is not None:
            post.stage = STAGE_DONE
            self._commit()
        self._remember(sent_ids[0], username, out.body or "\n".join(out.extras) or "[附件]")
        return sent_ids

    async def _post_recover(self, user_id, out, **kwargs):
        """发到用户当前的帖子；帖子被删了就重建（不再重复迁移历史）后重试一次"""
        thread_id = await self._ensure_thread(user_id, history=False)
        try:
            return await self._post(user_id, thread_id, out, **kwargs)
        except ThreadGone:
            self._mark_thread_gone(thread_id)
            thread_id = await self._ensure_thread(user_id, history=False)
            return await self._post(user_id, thread_id, out, **kwargs)

    async def _system_post(self, user_id, text):
        await self._post_recover(user_id, OutMessage(bodies={0: text}), username="🤖 系统", origin="system", pairs=[])

    def _tg_sync_state(self, group_message_ids):
        """这些 TG 消息的同步状态：None 未同步，"done" 已完成，"partial" 上次只发了一部分"""
        ids = [g for g in group_message_ids if g]
        if not ids:
            return None
        rows = (
            self.db.query(DiscordMessageMap)
            .filter(DiscordMessageMap.group_chat_message_id.in_(ids), DiscordMessageMap.origin != "dc_admin")
            .all()
        )
        if not rows:
            return None
        post_ids = {r.post_id for r in rows if r.post_id}
        if not post_ids:
            return "done"
        unfinished = (
            self.db.query(DiscordPost)
            .filter(DiscordPost.id.in_(post_ids), DiscordPost.stage < STAGE_DONE)
            .count()
        )
        return "partial" if unfinished else "done"

    async def _discard_partial_tg(self, group_message_ids):
        """撤回上次只发了一部分的 Discord 消息，以便整条重发"""
        rows = (
            self.db.query(DiscordMessageMap)
            .filter(DiscordMessageMap.group_chat_message_id.in_(group_message_ids), DiscordMessageMap.origin != "dc_admin")
            .all()
        )
        post_ids = {r.post_id for r in rows if r.post_id}
        posts = self.db.query(DiscordPost).filter(DiscordPost.id.in_(post_ids), DiscordPost.stage < STAGE_DONE).all()
        for post in posts:
            rows = self.db.query(DiscordMessageMap).filter(DiscordMessageMap.post_id == post.id).all()
            for thread_id, message_id in {(r.discord_thread_id, r.discord_message_id) for r in rows}:
                await self._delete_dc_message(thread_id, message_id)
            for r in rows:
                self.db.delete(r)
            self.db.delete(post)
            self._commit()

    def _jump_url(self, thread_id, message_id):
        return f"https://discord.com/channels/{self.forum.guild.id}/{thread_id}/{message_id}"

    async def _reply_line(self, thread_id, message_id, quote=None):
        author, snippet = self._snippets.get(message_id, (None, None))
        if author is None:
            try:
                m = await self.client.get_partial_messageable(thread_id).fetch_message(message_id)
                author = m.author.display_name
                snippet = plain_snippet(m.content) or ("[附件]" if m.attachments else "")
                self._remember(message_id, author, m.content or "[附件]")
            except discord.HTTPException:
                pass
        if quote:
            snippet = plain_snippet(quote)
        who = f"**{escape_md(author)}**：" if author else ""
        return f"-# ↪️ 回复 {who}{escape_md(snippet or '')} [↗]({self._jump_url(thread_id, message_id)})"

    def _dmm_first(self, *conditions):
        """优先返回「TG 消息本身」那一行，其次是正文分段行"""
        return (
            self.db.query(DiscordMessageMap)
            .filter(*conditions)
            .order_by(DiscordMessageMap.text_role.isnot(None), DiscordMessageMap.part, DiscordMessageMap.id)
            .first()
        )

    async def _reply_prefix_for_tg(self, user_id, message, side):
        reply = message.reply_to_message
        if not reply:
            return []
        if side == "user":
            row = self._dmm_first(
                DiscordMessageMap.user_id == user_id,
                DiscordMessageMap.user_chat_message_id == reply.message_id,
            )
        else:
            row = self._dmm_first(DiscordMessageMap.group_chat_message_id == reply.message_id)
        if not row:
            return []
        quote = message.quote.text if getattr(message, "quote", None) else None
        return [await self._reply_line(row.discord_thread_id, row.discord_message_id, quote)]

    # ---------- Telegram 消息 → OutMessage ----------

    def _ptb_file(self, file_obj, *, kind, filename, label, source, group_msg_id, spoiler=False, duration=None):
        bot, mtproto = self.bot, self.mtproto

        async def fetch():
            f = await bot.get_file(file_obj.file_id)
            return bytes(await f.download_as_bytearray())

        async def fetch_to(path):
            await mtproto.download_group_media(group_msg_id, path)

        return OutFile(
            kind=kind,
            filename=filename,
            label=label,
            size=file_obj.file_size,
            fetch=fetch,
            fetch_to=fetch_to if (mtproto and group_msg_id) else None,
            spoiler=spoiler,
            duration=duration,
            source=source,
            link=tg_message_link(group_msg_id),
        )

    @staticmethod
    def _forward_name(origin):
        if origin is None:
            return None
        for attr in ("sender_user", "sender_chat", "chat"):
            obj = getattr(origin, attr, None)
            if obj is not None:
                return getattr(obj, "full_name", None) or getattr(obj, "title", None) or str(obj.id)
        return getattr(origin, "sender_user_name", None) or "隐藏用户"

    def out_from_ptb(self, messages, *, origin, pairs):
        out = OutMessage()
        for m in messages:
            source = next(
                (i for i, (u, g) in enumerate(pairs) if (u if origin == "user" else g) == m.message_id), 0
            )
            group_msg_id = pairs[source][1] if pairs else None

            def file(obj, kind, filename, label, spoiler=False, duration=None):
                return self._ptb_file(
                    obj,
                    kind=kind,
                    filename=filename,
                    label=label,
                    source=source,
                    group_msg_id=group_msg_id,
                    spoiler=spoiler,
                    duration=duration,
                )

            if m.text:
                out.bodies[source] = tg_to_discord(m.text, ents_from_ptb(m.entities))
            elif m.caption:
                out.bodies[source] = tg_to_discord(m.caption, ents_from_ptb(m.caption_entities))
            spoiler = bool(m.has_media_spoiler)
            if m.photo:
                out.files.append(file(m.photo[-1], "photo", "photo.jpg", "图片", spoiler))
            elif m.animation:
                a = m.animation
                out.files.append(file(a, "animation", a.file_name or "animation.mp4", "GIF", spoiler, a.duration))
            elif m.video:
                v = m.video
                out.files.append(file(v, "video", v.file_name or "video.mp4", "视频", spoiler, v.duration))
            elif m.video_note:
                vn = m.video_note
                out.files.append(file(vn, "video_note", "video_note.mp4", "视频消息", duration=vn.duration))
            elif m.voice:
                out.files.append(file(m.voice, "voice", "voice.ogg", "语音"))
            elif m.audio:
                out.files.append(file(m.audio, "audio", m.audio.file_name or "audio.mp3", "音频"))
                title = " - ".join(x for x in (m.audio.performer, m.audio.title) if x)
                if title:
                    out.extras.append(f"🎵 {escape_md(title)}")
            elif m.sticker:
                st = m.sticker
                if st.is_animated:
                    f = file(st, "sticker_tgs", "sticker.tgs", "贴纸")
                    if st.thumbnail:
                        f.fallback = file(st.thumbnail, "sticker", "sticker.webp", "贴纸")
                    out.files.append(f)
                elif st.is_video:
                    out.files.append(file(st, "sticker_video", "sticker.webm", "贴纸"))
                else:
                    out.files.append(file(st, "sticker", "sticker.webp", "贴纸"))
                if st.emoji:
                    out.extras.append(f"-# 贴纸 {st.emoji}")
            elif m.document:
                out.files.append(file(m.document, "document", m.document.file_name or "file", "文件"))
            elif m.venue:
                v = m.venue
                out.extras.append(render_venue(v.title, v.address, v.location.latitude, v.location.longitude))
            elif m.location:
                out.extras.append(render_location(m.location.latitude, m.location.longitude))
            elif m.contact:
                c = m.contact
                out.extras.append(render_contact(c.first_name, c.last_name, c.phone_number))
            elif m.poll:
                out.extras.append(render_poll(m.poll.question, [o.text for o in m.poll.options], m.poll.type == "quiz"))
            elif m.dice:
                out.extras.append(render_dice(m.dice.emoji, m.dice.value))
            elif m.story:
                chat = m.story.chat
                out.extras.append(render_story(chat.effective_name, chat.username, m.story.id))
            elif m.game:
                out.extras.append(render_game(m.game.title, m.game.description))
            elif m.invoice:
                i = m.invoice
                out.extras.append(render_invoice(i.title, i.description, i.total_amount, i.currency))
            elif m.giveaway:
                g = m.giveaway
                out.extras.append(render_giveaway(g.winner_count, g.winners_selection_date.timestamp(), g.prize_description))
            elif getattr(m, "paid_media", None):
                pm = m.paid_media
                out.extras.append(render_paid_media(len(pm.paid_media), pm.star_count))
            elif not (m.text or m.caption):
                attachment = m.effective_attachment
                kind = type(attachment).__name__ if attachment else "未知"
                out.extras.append(f"-# [不支持同步的消息类型：{kind}]")
        out.forward_from = self._forward_name(messages[0].forward_origin)
        return out

    def _label(self, user_id, origin, message=None, tg_user=None):
        if origin == "user":
            return sanitize_webhook_name(self._user_name(user_id, tg_user))
        if message is not None and message.sender_chat:
            name = message.author_signature or "匿名管理员"
        elif message is not None and message.from_user:
            name = message.from_user.full_name
        else:
            name = "管理员"
        return sanitize_webhook_name(f"🛡️ {name} · TG")

    # ---------- 由 TG 处理器调用（均不阻塞） ----------

    def stash_album_part(self, message):
        """媒体组消息先暂存，等 TG 侧整组转发完成后再一起同步"""
        if not self.active:
            return
        now = self._now()
        for key in [k for k, (t, _) in self._albums.items() if now - t > 300]:
            del self._albums[key]
        key = (message.chat.id, str(message.media_group_id))
        self._albums.setdefault(key, (now, []))[1].append(message)

    def pop_album(self, chat_id, media_group_id):
        _, messages = self._albums.pop((int(chat_id), str(media_group_id)), (0, []))
        return sorted(messages, key=lambda m: m.message_id)

    def _album_pending(self, message):
        return message.media_group_id and (message.chat.id, str(message.media_group_id)) in self._albums

    def _retry_edit_later(self, func, user_id, message, retries):
        """媒体组要等几秒才整组同步，此前到达的编辑稍后再试"""
        if retries < 3:
            self._later(6, lambda: func(user_id, message, _retries=retries + 1))
            return True
        return False

    def _is_new_conversation(self, user_id):
        """TG 话题是在启用 Discord 同步之后创建的，即新会话"""
        u = self.db.query(User).filter(User.user_id == user_id).first()
        return bool(u and u.message_thread_id and u.message_thread_id > self.history_cutoff)

    def mirror_tg_messages(self, user_id, messages, *, origin, pairs, tg_user=None):
        """origin: user（用户发来的）/ tg_admin（TG 管理员回复的）；pairs: [(用户侧消息ID, 群内消息ID)]"""
        if not self.active or not messages or not pairs:
            return
        pairs = [tuple(p) for p in pairs]
        # 首次启用时，机器人离线期间管理员发的消息 ID 不大于 cutoff，属于历史，由迁移负责
        if self.history_cutoff and all(g and g <= self.history_cutoff for _, g in pairs):
            return
        outbox_id = self._outbox_add(user_id, "tg", {"origin": origin, "pairs": pairs})
        group_ids = [g for _, g in pairs]

        async def job():
            state = self._tg_sync_state(group_ids)
            if state == "done":
                return
            if state == "partial":
                await self._discard_partial_tg(group_ids)
            announce = origin == "user" and self._is_new_conversation(user_id)
            await self._ensure_thread(user_id, tg_user=tg_user, announce=announce)
            out = self.out_from_ptb(messages, origin=origin, pairs=pairs)
            if origin == "user":
                out.bodies = {k: to_simplified(v) for k, v in out.bodies.items()}
                out.extras = [to_simplified(x) for x in out.extras]
            side = "user" if origin == "user" else "group"
            prefix = await self._reply_prefix_for_tg(user_id, messages[0], side)
            await self._post_recover(
                user_id,
                out,
                username=self._label(user_id, origin, messages[0], tg_user),
                prefix=prefix,
                origin=origin,
                pairs=pairs,
            )

        self._submit_outbox(user_id, outbox_id, job, f"tg→dc {origin}")

    def mirror_user_edit(self, user_id, message, _retries=0):
        """用户编辑了消息：和 TG 后台一样，以回复的形式贴出新内容（保留原文）"""
        if not self.active:
            return

        async def job():
            row = self._dmm_first(
                DiscordMessageMap.user_id == user_id,
                DiscordMessageMap.user_chat_message_id == message.message_id,
            )
            if not row:
                if self._album_pending(message):
                    self._retry_edit_later(self.mirror_user_edit, user_id, message, _retries)
                return
            if message.text is not None:
                body = tg_to_discord(message.text, ents_from_ptb(message.entities))
            else:
                body = tg_to_discord(message.caption or "", ents_from_ptb(message.caption_entities))
            body = to_simplified(body)
            if not body.strip():
                return
            jump = self._jump_url(row.discord_thread_id, row.discord_message_id)
            await self._post_recover(
                user_id,
                OutMessage(bodies={0: body}),
                username=self._label(user_id, "user"),
                prefix=[f"-# ✏️ 用户编辑了[这条消息]({jump})，新内容："],
                origin="system",
                pairs=[],
            )

        self._submit(user_id, job, "tg→dc user edit")

    def mirror_admin_edit(self, user_id, message, _retries=0):
        """TG 管理员编辑了回复：更新该条在帖子里的正文（媒体组会重建整组正文）"""
        if not self.active:
            return
        outbox_id = self._outbox_add(user_id, "tg_edit", {"group_msg_id": message.message_id}) if not _retries else None

        async def job():
            if message.text is not None:
                body = tg_to_discord(message.text, ents_from_ptb(message.entities))
            else:
                body = tg_to_discord(message.caption or "", ents_from_ptb(message.caption_entities))
            if not await self._apply_admin_edit(message.message_id, body):
                if self._album_pending(message):
                    self._retry_edit_later(self.mirror_admin_edit, user_id, message, _retries)

        self._submit_outbox(user_id, outbox_id, job, "tg→dc admin edit")

    async def _apply_admin_edit(self, group_msg_id, body):
        """更新某条 TG 消息在帖子里的正文并重建；找不到映射返回 False"""
        item = (
            self.db.query(DiscordMessageMap)
            .filter(
                DiscordMessageMap.group_chat_message_id == group_msg_id,
                DiscordMessageMap.text_role.is_(None),
                DiscordMessageMap.post_id.isnot(None),
            )
            .first()
        )
        if not item:
            return False
        item.body = body
        self._commit()
        await self._rebuild_post(item.post_id)
        return True

    async def _rebuild_post(self, post_id):
        """按当前各成员的正文重建帖子文字：编辑已有分段，多出的新发，少了的删除（带附件的只清空文字）。
        每一步都立即提交，并在结束时把保留下来的分段重新编号为 0..n-1"""
        post = self.db.get(DiscordPost, post_id)
        items = (
            self.db.query(DiscordMessageMap)
            .filter(DiscordMessageMap.post_id == post_id, DiscordMessageMap.text_role.is_(None))
            .order_by(DiscordMessageMap.id)
            .all()
        )
        if not post or not items:
            return
        parts = split_discord(_compose(post.header, [r.body or "" for r in items], post.footer))
        texts = (
            self.db.query(DiscordMessageMap)
            .filter(DiscordMessageMap.post_id == post_id, DiscordMessageMap.text_role == "text")
            .order_by(DiscordMessageMap.part, DiscordMessageMap.id)
            .all()
        )
        first = items[0]
        if not texts:
            # 原来只有附件没有文字：把文字放到第一条附件消息上
            row = DiscordMessageMap(
                user_id=first.user_id,
                user_chat_message_id=first.user_chat_message_id,
                group_chat_message_id=first.group_chat_message_id,
                discord_message_id=first.discord_message_id,
                discord_thread_id=first.discord_thread_id,
                part=0,
                origin=first.origin,
                text_role="text",
                post_id=post_id,
                has_files=True,
            )
            self.db.add(row)
            self._commit()
            texts = [row]
        thread_id = post.thread_id
        kept = []
        for k, row in enumerate(texts):
            if k < len(parts):
                await self._edit_dc_message(thread_id, row.discord_message_id, parts[k], post.username)
                kept.append(row)
            elif row.has_files:
                await self._edit_dc_message(thread_id, row.discord_message_id, None, post.username)
                kept.append(row)
            else:
                await self._delete_dc_message(thread_id, row.discord_message_id)
                self.db.delete(row)
                self._commit()
        # 带附件但被清空文字的分段保留在原位置；新增的分段只能追加在后面
        for k in range(min(len(texts), len(parts)), len(parts)):
            message = await self._webhook_send(thread_id, content=parts[k], uploads=[], username=post.username)
            row = DiscordMessageMap(
                user_id=first.user_id,
                user_chat_message_id=first.user_chat_message_id,
                group_chat_message_id=first.group_chat_message_id,
                discord_message_id=message.id,
                discord_thread_id=thread_id,
                part=k,
                origin=first.origin,
                text_role="text",
                post_id=post_id,
            )
            self.db.add(row)
            self._commit()
            kept.append(row)
        for k, row in enumerate(kept):
            row.part = k
        self._commit()

    def mirror_tg_reaction(self, user_id, *, old, new, user_msg_id=None, group_msg_id=None):
        if not self.active:
            return
        old = [r.emoji for r in old if isinstance(r, ReactionTypeEmoji)]
        new = [r.emoji for r in new if isinstance(r, ReactionTypeEmoji)]
        added = [e for e in new if e not in old]
        removed = [e for e in old if e not in new]
        if not added and not removed:
            return

        async def job():
            if group_msg_id is not None:
                row = self._dmm_first(DiscordMessageMap.group_chat_message_id == group_msg_id)
            else:
                row = self._dmm_first(
                    DiscordMessageMap.user_id == user_id,
                    DiscordMessageMap.user_chat_message_id == user_msg_id,
                )
            if not row:
                return
            for e in removed:
                await self._dc_react(row.discord_thread_id, row.discord_message_id, e, add=False)
            for e in added:
                await self._dc_react(row.discord_thread_id, row.discord_message_id, e, add=True)

        self._submit(user_id, job, "tg→dc reaction")

    async def _dc_react(self, thread_id, message_id, emoji, *, add):
        message = self.client.get_partial_messageable(thread_id).get_partial_message(message_id)
        candidates = [emoji] if emoji.endswith("️") else [emoji, emoji + "️"]
        for candidate in candidates:
            for _ in range(2):
                try:
                    if add:
                        await message.add_reaction(candidate)
                    else:
                        await message.remove_reaction(candidate, self.client.user)
                    return
                except discord.HTTPException as e:
                    if e.code == DC_THREAD_ARCHIVED:
                        await self._unarchive(thread_id)
                        continue
                    if e.code == DC_UNKNOWN_EMOJI:
                        break
                    if e.code in (DC_UNKNOWN_MESSAGE, DC_UNKNOWN_CHANNEL, DC_THREAD_LOCKED):
                        return
                    raise
        logger.info(f"Discord 不支持该表情，跳过：{emoji!r}")
